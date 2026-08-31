#!/usr/bin/env python3
"""Safe helper for installing OpenWrt on a Cudy TR1200 v1.

The tool deliberately does not write raw MTD/SPI partitions. It supports:
* downloading and checksum-verifying official OpenWrt images;
* serving the initramfs image as U-Boot's recovery.bin over TFTP;
* uploading a sysupgrade image to an already-running OpenWrt router and
  invoking sysupgrade over SSH.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass


DEFAULT_RELEASE = "24.10.5"
DEFAULT_TARGET = "ramips/mt76x8"
DEFAULT_DEVICE = "cudy_tr1200-v1"
DOWNLOAD_ROOT = "https://downloads.openwrt.org/releases"
STOCK_DRIVE_ID = "1vqg9GMi3LVF6viG7gSDZf6XwlsD88kX_"
THEME_FILENAME = "luci-theme-openwrt-2020_26.228.65014~8e278ba_all.ipk"
THEME_SHA256 = "fa514b2df92363e253d5798de7242527db71cc5fcfd638ba7e4af588b52d061e"
ARGON_FILENAME = "luci-theme-argon_2.4.7_all.ipk"
ARGON_URL = "https://github.com/jerrykuku/luci-theme-argon/releases/download/v2.4.7/luci-theme-argon_2.4.7_all.ipk"
ARGON_SHA256 = "d0a5d0992f1e13094c89c29f46868a6bba79cdd644a0b4b606088267cb2e8e59"
ZH_FILENAME = "luci-i18n-base-zh-cn_26.228.65014~8e278ba_all.ipk"
ZH_SHA256 = "37a7131888fed872e55b38cd26c97ade9012efe50624db552ef487b6666d91d0"
WEB_HOST = "127.0.0.1"
WEB_PORT = 8765


class FlasherError(RuntimeError):
    """An actionable user-facing error."""


@dataclass(frozen=True)
class ImageInfo:
    kind: str
    filename: str
    url: str


class WebState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.phase = "idle"
        self.progress = 0
        self.logs: list[str] = []
        self.error: str | None = None
        self.result_path: str | None = None
        self.workflow: dict[str, object] = {
            "stage": "offline",
            "label": "检测中…",
            "host": "",
            "ssh": False,
        }

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
            self.logs = self.logs[-200:]

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            images = pathlib.Path("images")
            stock = images / "TR1200-OpenWRT-Flash.bin"
            sysupgrade = next(images.glob("*cudy_tr1200-v1-squashfs-sysupgrade.bin"), None) if images.is_dir() else None
            workflow = self.workflow
            stage = str(workflow["stage"])
            return {
                "busy": self.busy,
                "phase": self.phase,
                "progress": self.progress,
                "logs": self.logs,
                "error": self.error,
                "result_path": self.result_path,
                "stock_image": str(stock) if stock.is_file() else None,
                "sysupgrade_image": str(sysupgrade) if sysupgrade and sysupgrade.is_file() else None,
                "stage": stage,
                "stage_label": str(workflow["label"]),
                "host": str(workflow["host"]),
                "complete": bool(workflow.get("complete", False)),
                "stage_hint": (
                    "原厂阶段：输入管理员密码，工具自动定位并上传中间固件。"
                    if stage == "stock"
                    else "刷写已完成：正式 OpenWrt 已启动，可进行主题和中文配置。"
                    if workflow.get("complete")
                    else "OpenWrt 阶段：工具将把正式 sysupgrade 镜像写入闪存。"
                    if stage == "openwrt"
                    else "未检测到设备：请确认网线接 LAN 口并检查电脑网络地址。"
                ),
                "flash_hint": (
                    "原厂阶段：输入管理员密码，自动上传中间固件。"
                    if stage == "stock"
                    else "刷写已完成：无需重复刷写，请直接配置主题、中文和 Wi-Fi。"
                    if workflow.get("complete")
                    else "OpenWrt 阶段：刷写期间保持网线和供电，完成后再配置 Wi-Fi。"
                    if stage == "openwrt"
                    else "请先连接并检测路由器。"
                ),
            }


WEB_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TR1200 OpenWrt 刷机工具</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#f4f6f8;color:#17202a;margin:0}
main{max-width:820px;margin:32px auto;padding:0 18px}h1{margin-bottom:6px}
.muted{color:#667085}.card{background:#fff;border:1px solid #dfe3e8;border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 2px 8px #17202a0d}
button{background:#0969da;color:#fff;border:0;border-radius:7px;padding:10px 16px;font-size:15px;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}input{padding:9px;border:1px solid #b9c2cc;border-radius:6px}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.bar{height:12px;background:#e5e7eb;border-radius:8px;overflow:hidden;margin:12px 0}.fill{height:100%;background:#2da44e;width:0;transition:width .3s}
pre{background:#10151c;color:#d1f7d6;padding:14px;border-radius:8px;min-height:150px;max-height:280px;overflow:auto;white-space:pre-wrap}
.ok{color:#1a7f37}.bad{color:#cf222e}.warn{background:#fff8c5;padding:10px;border-radius:7px}
</style></head><body><main>
<h1>TR1200 OpenWrt 刷机工具</h1><p class="muted">本地网页控制台，仅支持 Cudy TR1200 v1（R46）。</p>
<div class="card"><h2>1. 自动检测</h2><div class="row"><label>检测到的阶段 <input id="stageLabel" value="检测中…" readonly></label><label>路由器地址 <input id="host" value="" placeholder="自动识别" readonly></label><button onclick="detect()">重新检测</button></div><p id="stageHint" class="warn">正在检测原厂和 OpenWrt 地址…</p><p id="detectResult" class="muted">尚未检测</p></div>
<div class="card"><h2>2. 准备固件</h2><div class="row"><label>版本 <input id="release" value="24.10.5"></label><button onclick="downloadStock()">下载 Cudy 中间固件</button><button onclick="downloadImage()">下载并校验 OpenWrt sysupgrade</button></div><p id="imageResult" class="muted">正在检查本地 images 文件夹…</p></div>
<div class="card" id="flashCard"><h2>3. 开始刷写</h2><p id="flashHint" class="warn">原厂阶段：输入管理员密码。工具会自动定位已下载的 TR1200-OpenWRT-Flash.bin、登录并上传。</p>
<div class="row" id="passwordRow"><label>管理员密码（默认：admin） <input id="password" type="password" placeholder="登录网页用，不是 Wi-Fi 密码"></label><span id="stockStatus" class="muted"></span></div>
<div class="row"><label>OpenWrt 镜像路径 <input id="image" size="56" placeholder="OpenWrt 阶段选择 sysupgrade 镜像"></label><label><input type="checkbox" id="keep"> 保留设置</label><button id="flash" onclick="flash()">自动上传中间固件</button></div>
<div class="bar"><div id="fill" class="fill"></div></div><p id="phase" class="muted">空闲</p></div>
<div class="card"><h2>4. 刷写完成后配置</h2><p class="muted">仅在正式 OpenWrt 已启动后执行。工具会自动下载、校验并安装 Argon 主题和简体中文。</p><button onclick="installTheme()">安装并切换 Argon + 中文</button><span id="themeResult" class="muted"></span></div>
<div class="card"><div class="row"><h2 style="margin-right:auto">实时日志</h2><button onclick="copyLogs()">复制日志</button><span id="copyResult" class="muted"></span></div><pre id="logs">等待操作…</pre></div>
<script>
const $=id=>document.getElementById(id);
async function call(path,options){const r=await fetch(path,options);const j=await r.json();if(!r.ok)throw Error(j.error||"请求失败");return j}
function stageChanged(){return}
async function detect(){ $("detectResult").textContent="检测中…"; try{const j=await call("/api/detect?host="+encodeURIComponent($("host").value));$("detectResult").innerHTML=j.reachable?'<span class="ok">已发现可刷写设备</span>：'+j.details:(j.details.includes("HTTP")?'<span class="warn">已发现设备，但当前阶段不能执行 SSH 刷写</span>：'+j.details:'<span class="bad">未发现服务</span>：'+j.details)}catch(e){$("detectResult").textContent=e.message}}
async function downloadImage(){ $("imageResult").textContent="下载和校验中…"; try{await call("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({release:$("release").value})});$("imageResult").textContent="后台下载和校验中，请查看进度与日志…"}catch(e){$("imageResult").textContent=e.message}}
async function downloadStock(){ $("imageResult").textContent="下载 Cudy 中间固件中…"; try{const j=await call("/api/stock-download",{method:"POST"});$("imageResult").textContent="后台下载中，完成后自动出现在文件选择框…"}catch(e){$("imageResult").textContent=e.message}}
async function installTheme(){ $("themeResult").textContent="后台下载、校验和安装中…"; try{await call("/api/theme-install",{method:"POST"});$("themeResult").textContent="已提交安装，请查看日志"}catch(e){$("themeResult").textContent=e.message}}
async function flash(){const stage=$("stageLabel").dataset.stage;if(stage==="stock"){if(!$("password").value){alert("请填写管理员密码；默认是 admin，不是 Wi-Fi 密码");return}if(!confirm("确认自动登录原厂固件并上传已下载的中间固件？"))return;$("flash").disabled=true;try{await call("/api/stock-flash",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({host:$("host").value,password:$("password").value})})}catch(e){alert(e.message);$("flash").disabled=false}return} if(stage!=="openwrt"){alert("尚未检测到可用设备");return} if(!confirm("确认将中间 OpenWrt 升级为正式 OpenWrt？\\n工具会保留正确的 TR1200 v1 兼容检查。"))return; $("flash").disabled=true;try{await call("/api/flash",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({host:$("host").value,image:$("image").value,keep_settings:$("keep").checked})})}catch(e){alert(e.message);$("flash").disabled=false}}
async function copyLogs(){const text=$("logs").textContent||"";try{await navigator.clipboard.writeText(text);$("copyResult").textContent="已复制"}catch(e){const area=document.createElement("textarea");area.value=text;document.body.appendChild(area);area.select();document.execCommand("copy");area.remove();$("copyResult").textContent="已复制"}setTimeout(()=>{$("copyResult").textContent=""},1800)}
async function refresh(){try{const j=await call("/api/status");$("fill").style.width=j.progress+"%";$("phase").textContent=j.error?"失败："+j.error:(j.busy?j.phase+" ("+j.progress+"%)":"空闲");$("logs").textContent=j.logs.join("\\n")||"等待操作…";if(j.sysupgrade_image&&!$("image").value){$("image").value=j.sysupgrade_image} if(j.stock_image){$("stockStatus").innerHTML='<span class="ok">中间固件已下载</span>：'+j.stock_image;$("imageResult").innerHTML='<span class="ok">中间固件已下载</span>：'+j.stock_image}else if(!j.busy&&j.error){$("stockStatus").innerHTML='<span class="bad">下载失败</span>';$("imageResult").innerHTML='<span class="bad">中间固件未下载</span>：'+j.error}else if(!j.busy){$("stockStatus").textContent="中间固件未下载";$("imageResult").textContent="中间固件未下载；点击下载按钮获取"} if(j.stage){$("stageLabel").value=j.stage_label;$("stageLabel").dataset.stage=j.stage;$("host").value=j.host;$("passwordRow").style.display=j.stage==="stock"?"flex":"none";$("stageHint").textContent=j.stage_hint;$("flashHint").textContent=j.flash_hint;$("flash").textContent=j.stage==="stock"?"自动上传中间固件":"开始 sysupgrade";$("flashCard").style.display=j.complete?"none":"block"} $("flash").disabled=j.stage==="offline"||j.busy||(j.stage==="openwrt"&&!$("image").value.trim())||(j.stage==="stock"&&!j.stock_image||j.complete)}catch(e){}}
setInterval(refresh,700);refresh();
</script></main></body></html>"""


def image_info(release: str, kind: str) -> ImageInfo:
    if kind == "sysupgrade":
        suffix = "squashfs-sysupgrade.bin"
    elif kind == "initramfs":
        suffix = "squashfs-initramfs-kernel.bin"
    else:
        raise FlasherError(f"Unsupported image type: {kind}")
    filename = f"openwrt-{release}-{DEFAULT_TARGET.replace('/', '-')}-{DEFAULT_DEVICE}-{suffix}"
    url = f"{DOWNLOAD_ROOT}/{release}/targets/{DEFAULT_TARGET}/{filename}"
    return ImageInfo(kind, filename, url)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tr1200-flasher/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FlasherError(f"Download failed for {url}: {exc}") from exc


def download_verified(info: ImageInfo, output_dir: pathlib.Path) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / info.filename
    print(f"Downloading {info.url}")
    image = fetch(info.url)
    checksum_url = f"{info.url.rsplit('/', 1)[0]}/sha256sums"
    checksums = fetch(checksum_url).decode("utf-8", errors="replace")
    expected = None
    for line in checksums.splitlines():
        fields = line.split()
        if len(fields) >= 2 and pathlib.PurePath(fields[-1].lstrip("*")).name == info.filename:
            expected = fields[0]
            break
    if expected is None:
        raise FlasherError(f"{info.filename} is not listed in {checksum_url}")
    actual = hashlib.sha256(image).hexdigest()
    if actual.lower() != expected.lower():
        raise FlasherError(f"SHA-256 mismatch: expected {expected}, got {actual}")
    destination.write_bytes(image)
    print(f"Saved and verified: {destination} ({actual})")
    return destination


def download_stock_intermediate(output_dir: pathlib.Path) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "TR1200-OpenWRT-Flash.bin"
    archive = output_dir / "TR1200 V1.zip"
    urls = (
        f"https://drive.google.com/uc?export=download&id={STOCK_DRIVE_ID}",
        f"https://drive.usercontent.google.com/download?id={STOCK_DRIVE_ID}&export=download&confirm=t",
    )
    last_error: Exception | None = None
    for url in urls:
        try:
            result = subprocess.run(
                ["curl.exe", "-L", "--fail", "--max-time", "120", "-A", "Mozilla/5.0", "-o", str(archive), url],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise FlasherError(result.stderr.strip() or f"curl exited with code {result.returncode}")
            with zipfile.ZipFile(archive):
                pass
            break
        except (FlasherError, zipfile.BadZipFile) as exc:
            last_error = exc
    else:
        raise FlasherError(f"无法下载 Cudy 官方中间固件：{last_error}")
    try:
        with zipfile.ZipFile(archive) as bundle:
            member = next((name for name in bundle.namelist() if pathlib.PurePath(name).name == destination.name), None)
            if member is None:
                raise FlasherError("官方压缩包中未找到 TR1200-OpenWRT-Flash.bin")
            destination.write_bytes(bundle.read(member))
    except zipfile.BadZipFile as exc:
        raise FlasherError("中间固件下载结果不是有效 ZIP 文件，请检查网络后重试") from exc
    archive.unlink(missing_ok=True)
    return destination


def download_theme(output_dir: pathlib.Path) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / THEME_FILENAME
    url = f"{DOWNLOAD_ROOT}/24.10.5/packages/mipsel_24kc/luci/{THEME_FILENAME}"
    path.write_bytes(fetch(url))
    if sha256(path).lower() != THEME_SHA256:
        path.unlink(missing_ok=True)
        raise FlasherError("LuCI 主题 SHA-256 校验失败")
    return path


def install_theme(host: str, package: pathlib.Path) -> None:
    remote = f"/tmp/{package.name}"
    common = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no"]
    result = subprocess.run(
        ["scp.exe", *common, "-O", str(package), f"root@{host}:{remote}"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode:
        raise FlasherError(f"主题上传失败：{result.stderr.strip()}")
    command = f"opkg install {remote} && uci set luci.main.mediaurlbase='/luci-static/openwrt2020' && uci commit luci && rm -f {remote}"
    result = subprocess.run(
        ["ssh.exe", *common, f"root@{host}", command],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise FlasherError(f"主题安装失败：{result.stderr.strip()}")


def prepare_postflash_packages(output_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    argon = output_dir / ARGON_FILENAME
    if not argon.is_file() or sha256(argon).lower() != ARGON_SHA256:
        result = subprocess.run(
            ["curl.exe", "-L", "--fail", "--max-time", "60", "-o", str(argon), ARGON_URL],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise FlasherError(f"Argon 下载失败：{result.stderr.strip()}")
    if sha256(argon).lower() != ARGON_SHA256:
        raise FlasherError("Argon 主题 SHA-256 校验失败")
    zh = output_dir / ZH_FILENAME
    if not zh.is_file():
        url = f"{DOWNLOAD_ROOT}/24.10.5/packages/mipsel_24kc/luci/{ZH_FILENAME}"
        zh.write_bytes(fetch(url))
    if sha256(zh).lower() != ZH_SHA256:
        raise FlasherError("简体中文语言包 SHA-256 校验失败")
    return argon, zh


def install_postflash_packages(host: str, output_dir: pathlib.Path) -> None:
    argon, zh = prepare_postflash_packages(output_dir)
    common = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no"]
    for package in (argon, zh):
        result = subprocess.run(
            ["scp.exe", *common, "-O", str(package), f"root@{host}:/tmp/{package.name}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode:
            raise FlasherError(f"上传 {package.name} 失败：{result.stderr.strip()}")
    command = (
        f"opkg install /tmp/{argon.name} /tmp/{zh.name} && "
        "uci set luci.main.mediaurlbase='/luci-static/argon' && "
        "uci set luci.main.lang='zh_cn' && uci commit luci && "
        f"rm -f /tmp/{argon.name} /tmp/{zh.name}"
    )
    result = subprocess.run(
        ["ssh.exe", *common, f"root@{host}", command],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise FlasherError(f"主题/中文安装失败：{result.stderr.strip()}")


def install_theme_job(job: WebState) -> None:
    with job.lock:
        job.busy, job.phase, job.progress, job.error = True, "检查正式 OpenWrt", 5, None
        job.logs.clear()
    try:
        board = remote_board_info("192.168.1.1")
        if board.get("model") != "Cudy TR1200 v1" or board.get("board_name") != "cudy,tr1200-v1":
            raise FlasherError("当前不是已完成刷写的正式 Cudy TR1200 v1")
        with job.lock:
            job.phase, job.progress = "下载并安装 Argon + 中文", 20
        job.log("开始下载、校验并安装 Argon 主题和简体中文")
        install_postflash_packages("192.168.1.1", pathlib.Path("images"))
        with job.lock:
            job.phase, job.progress = "主题配置完成", 100
        job.log("Argon 主题和简体中文已启用")
    except Exception as exc:
        with job.lock:
            job.error = str(exc)
        job.log(f"错误：{exc}")
    finally:
        with job.lock:
            job.busy = False


def serve_tftp(directory: pathlib.Path, host: str, port: int) -> None:
    """Serve recovery.bin over a minimal read-only TFTP server."""
    import socket

    root = directory.resolve()
    image = root / "recovery.bin"
    if not image.is_file():
        raise FlasherError(f"Missing {image}; prepare an initramfs image first")

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((host, port))
    print(f"TFTP serving {image} on {host}:{port}. Press Ctrl+C to stop.")
    try:
        while True:
            packet, address = server.recvfrom(2048)
            if len(packet) < 4 or packet[:2] != b"\x00\x01":
                continue
            filename = packet[2:].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
            if pathlib.PurePath(filename).name != "recovery.bin":
                continue
            block_size = 512
            with image.open("rb") as stream:
                block = 1
                while True:
                    data = stream.read(block_size)
                    server.sendto(b"\x00\x03" + block.to_bytes(2, "big") + data, address)
                    response, _ = server.recvfrom(2048)
                    if response[:2] != b"\x00\x04":
                        raise FlasherError("Unexpected TFTP response")
                    if len(data) < block_size:
                        break
                    block = (block + 1) & 0xFFFF
    except KeyboardInterrupt:
        print("\nTFTP server stopped.")
    finally:
        server.close()


def prepare_recovery(image: pathlib.Path, output_dir: pathlib.Path) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    recovery = output_dir / "recovery.bin"
    recovery.write_bytes(image.read_bytes())
    print(f"Prepared {recovery}; configure the router's U-Boot TFTP request to recovery.bin.")
    return recovery


def ssh_upgrade(
    host: str,
    user: str,
    image: pathlib.Path,
    port: int,
    keep_settings: bool,
    force_known_tr1200_variant: bool = False,
) -> None:
    if not image.is_file():
        raise FlasherError(f"Image does not exist: {image}")
    if DEFAULT_DEVICE not in image.name or "squashfs-sysupgrade.bin" not in image.name:
        raise FlasherError(
            f"{image.name} is not a verified {DEFAULT_DEVICE} sysupgrade image; "
            "use the download command to obtain the matching image"
        )
    if image.name.startswith("openwrt-24.10.5-"):
        expected_hash = "a9115119724afa5c4b83721772316f9d6d3ebbb7fa0614714846d5cb446f4728"
        if sha256(image).lower() != expected_hash:
            raise FlasherError("sysupgrade 镜像 SHA-256 校验失败，拒绝刷写")
    remote = f"/tmp/{image.name}"
    ssh_target = f"{user}@{host}"
    common = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1", "-o", "StrictHostKeyChecking=no"]
    scp = ["scp", *common, "-O", "-P", str(port), str(image), f"{ssh_target}:{remote}"]
    ssh = ["ssh", *common, "-p", str(port), ssh_target, "sysupgrade"]
    if not keep_settings:
        ssh.append("-n")
    if force_known_tr1200_variant:
        ssh.append("-F")
    ssh.append(remote)
    print(f"Uploading {image} to {ssh_target}:{remote}")
    try:
        result = subprocess.run(scp, check=False, timeout=120, capture_output=True, text=True)
        if result.returncode:
            detail = result.stderr.strip() or f"scp exited with code {result.returncode}"
            raise FlasherError(f"SCP 上传失败：{detail}")
    except subprocess.TimeoutExpired as exc:
        raise FlasherError("SCP 上传超时；请确认 SSH 已启用且 root 登录不需要交互式密码") from exc
    print("Starting sysupgrade; do not remove power or Ethernet.")
    try:
        result = subprocess.run(ssh, check=False, timeout=30, capture_output=True, text=True)
        if result.returncode:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            if "Commencing upgrade" in output or "Closing all shell sessions" in output:
                print("sysupgrade started; the router closed SSH as expected.")
                return
            detail = output or f"ssh exited with code {result.returncode}"
            raise FlasherError(f"sysupgrade 执行失败：{detail}")
    except subprocess.TimeoutExpired as exc:
        raise FlasherError("sysupgrade 命令超时；请检查路由器 SSH 状态") from exc


def remote_board_info(host: str) -> dict[str, object]:
    result = subprocess.run(
        ["ssh.exe", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "ubus", "call", "system", "board"],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    if result.returncode:
        raise FlasherError(f"无法读取路由器型号：{result.stderr.strip() or 'SSH 连接失败'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FlasherError("路由器返回的型号信息无效") from exc


def _hidden_fields(html: str) -> dict[str, str]:
    return dict(re.findall(r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)', html, re.I))


def _multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----tr1200{hashlib.sha256(content[:64]).hexdigest()[:20]}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode(), b"\r\n"])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{pathlib.PurePath(filename).name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n", content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def stock_upgrade(host: str, password: str, image: pathlib.Path, progress: callable) -> None:
    if not image.is_file() or image.name != "TR1200-OpenWRT-Flash.bin":
        raise FlasherError("请选择文件名必须为 TR1200-OpenWRT-Flash.bin 的官方中间固件")
    base = f"http://{host}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login_url = f"{base}/cgi-bin/luci/"
    def open_page(request: urllib.request.Request | str) -> tuple[int, bytes]:
        try:
            response = opener.open(request, timeout=15)
            return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return exc.code, exc.read()
            raise

    login_status, login_body = open_page(
        urllib.request.Request(login_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    )
    login_html = login_body.decode("utf-8", errors="replace")
    if "luci_password_login" not in login_html:
        raise FlasherError(f"原厂登录页返回异常（HTTP {login_status}）")
    fields = _hidden_fields(login_html)
    salt = fields.get("salt")
    if not salt:
        raise FlasherError("未找到原厂页面的登录 salt")
    token_status, token_body = open_page(
        urllib.request.Request(
            f"{base}/cgi-bin/luci/admin/get_token",
            data=b"",
            headers={"User-Agent": "Mozilla/5.0", "Referer": login_url},
            method="POST",
        )
    )
    token = token_body.decode("utf-8").strip().strip('"')
    if token_status != 200:
        raise FlasherError(f"未取得原厂登录 token（HTTP {token_status}）")
    if not token:
        raise FlasherError("未取得原厂登录 token")
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    fields.update({"luci_username": "admin", "luci_password": hashlib.sha256((hashed + token).encode()).hexdigest(), "token": token})
    fields.setdefault("luci_language", "auto")
    fields.setdefault("zonename", "Asia/Shanghai")
    fields.setdefault("timeclock", str(int(time.time())))
    progress(20, "登录原厂固件")
    body = urllib.parse.urlencode(fields).encode()
    login_status, login_body = open_page(
        urllib.request.Request(
            login_url,
            data=body,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": login_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    )
    if "luci_password_login" in login_body.decode("utf-8", errors="replace"):
        raise FlasherError("原厂登录失败，请检查管理员密码")
    progress(45, "打开原厂升级接口")
    link_candidates = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', login_body.decode("utf-8", errors="replace"), re.I | re.S)
    upgrade_href = next(
        (
            href
            for href, label in link_candidates
            if re.search(r"upgrade|firmware|flash", re.sub(r"<[^>]+>", " ", label), re.I)
        ),
        None,
    )
    routes = []
    if upgrade_href:
        routes.append(urllib.parse.urljoin(login_url, upgrade_href))
    routes.extend(
        urllib.parse.urljoin(login_url, path)
        for path in (
            "/cgi-bin/luci/admin/system/upgrade",
            "/cgi-bin/luci/admin/system/flash",
            "/cgi-bin/luci/admin/system/firmware",
            "/cgi-bin/luci/admin/system/upgrade/flash",
        )
    )
    flash_url = ""
    flash_html = ""
    for candidate in dict.fromkeys(routes):
        flash_status, flash_body = open_page(
            urllib.request.Request(candidate, headers={"User-Agent": "Mozilla/5.0", "Referer": login_url})
        )
        candidate_html = flash_body.decode("utf-8", errors="replace")
        if "luci_password_login" in candidate_html:
            raise FlasherError("原厂会话已失效，无法进入升级页面")
        if re.search(r'type=["\']file["\']', candidate_html, re.I):
            flash_url, flash_html = candidate, candidate_html
            break
    if not flash_url:
        raise FlasherError("未找到原厂固件升级页面，请确认当前固件版本支持网页升级")
    form_match = re.search(r"<form\b([^>]*)>(.*?)</form>", flash_html, re.I | re.S)
    if not form_match:
        raise FlasherError("未找到原厂固件上传表单")
    form_attributes, form_content = form_match.groups()
    action_match = re.search(r'\baction=["\']([^"\']+)["\']', form_attributes, re.I)
    file_match = re.search(r'<input\b(?=[^>]*\btype=["\']file["\'])(?=[^>]*\bname=["\']([^"\']+)["\'])[^>]*>', form_content, re.I | re.S)
    if not action_match or not file_match:
        raise FlasherError("未找到原厂固件上传表单，设备固件界面可能不同")
    action = urllib.parse.urljoin(flash_url, action_match.group(1))
    upload_fields = _hidden_fields(form_content)
    file_field = file_match.group(1)
    upload_fields.update({
        f"{file_field}.upload": "true",
        "cbi.submit": "1",
    })
    upload_body, content_type = _multipart(upload_fields, file_field, image.name, image.read_bytes())
    progress(65, "上传签名中间固件")
    result = opener.open(urllib.request.Request(action, data=upload_body, headers={"Content-Type": content_type}), timeout=60)
    if result.status not in (200, 302):
        raise FlasherError(f"原厂上传失败，HTTP {result.status}")
    progress(70, "中间固件已提交，等待路由器写入并重启")


def router_probe(host: str) -> tuple[bool, str]:
    reachable: list[int] = []
    for port in (22, 80):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            sock.connect((host, port))
            reachable.append(port)
        except OSError:
            continue
        finally:
            sock.close()
    if 22 in reachable:
        return True, f"{host}: SSH(22) 可连接" + ("，HTTP(80) 可连接" if 80 in reachable else "")
    if 80 in reachable:
        return False, f"{host}: HTTP(80) 可连接，但 SSH(22) 未开放；不能执行网页 sysupgrade"
    return False, f"{host} 的 SSH(22) 和 HTTP(80) 均不可连接"


def detect_workflow() -> dict[str, object]:
    stock_http = _port_open("192.168.10.1", 80)
    openwrt_http = _port_open("192.168.1.1", 80)
    openwrt_ssh = _port_open("192.168.1.1", 22)
    if openwrt_ssh:
        label = "OpenWrt（已启用 SSH）"
        version = ""
        board_name = ""
        try:
            board = remote_board_info("192.168.1.1")
            version = str(board.get("release", {}).get("version", ""))
            board_name = str(board.get("board_name", ""))
            if version == "23.05.3":
                label = "OpenWrt 中间固件（23.05.3）"
            elif version:
                label = f"刷写已完成：正式 OpenWrt（{version}）"
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return {"stage": "openwrt", "label": label, "host": "192.168.1.1", "ssh": True, "board_name": board_name, "complete": version not in ("", "23.05.3")}
    if openwrt_http:
        return {"stage": "openwrt", "label": "OpenWrt（等待 SSH）", "host": "192.168.1.1", "ssh": False}
    if stock_http:
        return {"stage": "stock", "label": "Cudy 原厂系统", "host": "192.168.10.1", "ssh": False}
    return {"stage": "offline", "label": "未检测到设备", "host": "", "ssh": False}


def wait_for_router_transition(
    job: WebState,
    stock_host: str,
    openwrt_host: str,
    timeout: int = 240,
    require_offline: bool = False,
) -> None:
    started = time.monotonic()
    saw_offline = False
    last_state = ""
    while time.monotonic() - started < timeout:
        stock_http = _port_open(stock_host, 80)
        openwrt_http = _port_open(openwrt_host, 80)
        openwrt_ssh = _port_open(openwrt_host, 22)
        if not stock_http and not openwrt_http:
            state = "设备离线，正在写入并重启"
            saw_offline = True
        elif openwrt_ssh and (saw_offline or not require_offline):
            state = "OpenWrt 已启动，SSH(22) 可用"
            with job.lock:
                job.progress, job.phase = 100, state
            if state != last_state:
                job.log(state)
            return
        elif openwrt_http:
            state = "192.168.1.1 已恢复，等待 OpenWrt SSH"
        else:
            state = "等待设备重启"
        if state != last_state:
            job.log(state)
            last_state = state
        elapsed = time.monotonic() - started
        with job.lock:
            job.progress = min(99, 70 + int(elapsed / timeout * 29))
            job.phase = state
        time.sleep(3)
    raise FlasherError(f"等待设备恢复超时（{timeout} 秒）；请手动检查 192.168.1.1")


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.2)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_web(host: str, port: int) -> None:
    if host not in ("127.0.0.1", "localhost"):
        raise FlasherError("网页刷机服务只允许监听本机地址（127.0.0.1）")
    state = WebState()
    def refresh_workflow() -> None:
        while True:
            try:
                workflow = detect_workflow()
                with state.lock:
                    state.workflow = workflow
            except Exception as exc:
                state.log(f"自动检测失败：{exc}")
            time.sleep(5)

    threading.Thread(target=refresh_workflow, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def send_json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                body = WEB_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                self.send_json(state.snapshot())
                return
            if self.path.startswith("/api/detect"):
                from urllib.parse import parse_qs, urlparse
                query = parse_qs(urlparse(self.path).query)
                target = query.get("host", ["192.168.1.1"])[0]
                reachable, details = router_probe(target)
                state.log(("发现路由器：" if reachable else "未发现路由器：") + details)
                self.send_json({"reachable": reachable, "details": details})
                return
            self.send_json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            if self.path not in ("/api/download", "/api/flash", "/api/stock-flash", "/api/stock-download", "/api/theme-install"):
                self.send_json({"error": "Not found"}, 404)
                return
            if self.path == "/api/theme-install":
                with state.lock:
                    if state.busy:
                        self.send_json({"error": "已有任务正在运行"}, 409)
                        return
                    state.busy = True
                threading.Thread(target=install_theme_job, args=(state,), daemon=True).start()
                self.send_json({"started": True})
                return
            if self.path == "/api/stock-download":
                with state.lock:
                    if state.busy:
                        self.send_json({"error": "已有任务正在运行"}, 409)
                        return
                    state.busy = True
                thread = threading.Thread(target=stock_download_job, args=(state,), daemon=True)
                thread.start()
                self.send_json({"started": True})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if self.path == "/api/stock-flash":
                content_type = self.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    self.send_json({"error": "请先下载官方中间固件"}, 400)
                    return
                try:
                    fields = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self.send_json({"error": "请求 JSON 无效"}, 400)
                    return
                host_value = str(fields.get("host", "192.168.10.1"))
                password_value = str(fields.get("password", ""))
                upload_path = pathlib.Path("images") / "TR1200-OpenWRT-Flash.bin"
                if host_value != "192.168.10.1":
                    self.send_json({"error": "原厂阶段只允许连接 192.168.10.1"}, 400)
                    return
                if not password_value:
                    self.send_json({"error": "请填写管理员密码"}, 400)
                    return
                if not upload_path.is_file():
                    self.send_json({"error": "中间固件未下载，请先点击“下载 Cudy 中间固件”"}, 400)
                    return
                with state.lock:
                    if state.busy:
                        self.send_json({"error": "已有任务正在运行"}, 409)
                        return
                    state.busy = True
                thread = threading.Thread(
                    target=stock_flash_job,
                    args=(state, host_value, password_value, upload_path),
                    daemon=True,
                )
                thread.start()
                self.send_json({"started": True})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_json({"error": "请求 JSON 无效"}, 400)
                return
            if self.path == "/api/download":
                release = str(payload.get("release", DEFAULT_RELEASE))
                thread = threading.Thread(target=download_job, args=(state, release), daemon=True)
            else:
                image = pathlib.Path(str(payload.get("image", ""))).resolve()
                target = str(payload.get("host", "192.168.1.1"))
                if target != "192.168.1.1":
                    self.send_json({"error": "只允许操作当前检测到的 192.168.1.1"}, 400)
                    return
                images_root = pathlib.Path("images").resolve()
                if images_root not in image.parents:
                    self.send_json({"error": "镜像必须来自本工具 images 文件夹"}, 400)
                    return
                keep = bool(payload.get("keep_settings", False))
                thread = threading.Thread(target=flash_job, args=(state, target, image, keep), daemon=True)
            with state.lock:
                if state.busy:
                    self.send_json({"error": "已有任务正在运行"}, 409)
                    return
                state.busy = True
            thread.start()
            self.send_json({"started": True})

        def log_message(self, format: str, *args: object) -> None:
            return

    def download_job(job: WebState, release: str) -> None:
        with job.lock:
            job.busy, job.phase, job.progress, job.error, job.result_path = True, "下载镜像", 10, None, None
            job.logs.clear()
        try:
            info = image_info(release, "sysupgrade")
            job.log(f"下载 {info.filename}")
            path = download_verified(info, pathlib.Path("images"))
            with job.lock:
                job.progress, job.phase = 100, f"完成：{path}"
                job.result_path = str(path)
            job.log(f"SHA-256 校验通过：{sha256(path)}")
        except Exception as exc:
            with job.lock:
                job.error = str(exc)
            job.log(f"错误：{exc}")
        finally:
            with job.lock:
                job.busy = False

    def stock_download_job(job: WebState) -> None:
        with job.lock:
            job.busy, job.phase, job.progress, job.error, job.result_path = True, "下载 Cudy 中间固件", 10, None, None
            job.logs.clear()
        try:
            job.log("从 Cudy 官方 Google Drive 下载 TR1200 V1.zip")
            path = download_stock_intermediate(pathlib.Path("images"))
            with job.lock:
                job.progress, job.phase, job.result_path = 100, f"完成：{path}", str(path)
            job.log(f"已提取并检查文件名：{path.name}")
        except Exception as exc:
            with job.lock:
                job.error = str(exc)
            job.log(f"错误：{exc}")
        finally:
            with job.lock:
                job.busy = False

    def flash_job(job: WebState, target: str, image: pathlib.Path, keep: bool) -> None:
        with job.lock:
            job.busy, job.phase, job.progress, job.error, job.result_path = True, "检测路由器", 5, None, None
            job.logs.clear()
        try:
            reachable, details = router_probe(target)
            job.log(details)
            if not reachable:
                raise FlasherError("路由器不可连接，未执行刷写")
            board = remote_board_info(target)
            board_name = str(board.get("board_name", ""))
            model = str(board.get("model", ""))
            if model != "Cudy TR1200" or board_name not in ("cudy,tr1200", "cudy,tr1200-v1"):
                raise FlasherError(f"设备不匹配：model={model or '未知'} board={board_name or '未知'}")
            known_variant = board_name == "cudy,tr1200"
            with job.lock:
                job.phase, job.progress = "上传镜像", 20
            if known_variant:
                job.log("检测到官方中间固件的已知标识差异，将启用 TR1200 v1 兼容检查")
            ssh_upgrade(target, "root", image, 22, keep, force_known_tr1200_variant=known_variant)
            with job.lock:
                job.phase, job.progress = "sysupgrade 已启动，等待重启", 70
            job.log("sysupgrade 已开始；SSH 断开是正常现象，开始监控设备重启")
            wait_for_router_transition(job, target, "192.168.1.1", require_offline=True)
            with job.lock:
                job.phase, job.progress = "配置 Argon 和简体中文", 92
            job.log("正式 OpenWrt 已恢复，开始安装 Argon 主题和简体中文")
            install_postflash_packages("192.168.1.1", pathlib.Path("images"))
            with job.lock:
                job.phase, job.progress = "刷写与主题配置完成", 100
            job.log("Argon 主题和简体中文已启用，刷新 http://192.168.1.1/ 即可")
        except Exception as exc:
            with job.lock:
                job.error = str(exc)
            job.log(f"错误：{exc}")
        finally:
            with job.lock:
                job.busy = False

    def stock_flash_job(job: WebState, target: str, password: str, image: pathlib.Path) -> None:
        with job.lock:
            job.busy, job.phase, job.progress, job.error, job.result_path = True, "检测原厂路由器", 5, None, None
            job.logs.clear()
        try:
            reachable, details = router_probe(target)
            job.log(details)
            if "HTTP" not in details:
                raise FlasherError("原厂网页不可连接，请确认电脑已连接路由器并使用 192.168.10.1")
            def update_progress(value: int, phase: str) -> None:
                with job.lock:
                    job.progress, job.phase = value, phase
                job.log(phase)

            stock_upgrade(target, password, image, update_progress)
            job.log("原厂中间固件上传成功，开始监控写入、重启和 OpenWrt 启动")
            wait_for_router_transition(job, target, "192.168.1.1")
        except Exception as exc:
            with job.lock:
                job.error = str(exc)
            job.log(f"错误：{exc}")
        finally:
            with job.lock:
                job.busy = False

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"浏览器打开 http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb server stopped.")
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cudy TR1200 v1 OpenWrt flashing helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="download and verify an official image")
    download.add_argument("--release", default=DEFAULT_RELEASE, help=f"OpenWrt release (default: {DEFAULT_RELEASE})")
    download.add_argument("kind", choices=("sysupgrade", "initramfs"))
    download.add_argument("-o", "--output-dir", type=pathlib.Path, default=pathlib.Path("images"))

    prepare = subparsers.add_parser("prepare-recovery", help="rename an initramfs image to recovery.bin")
    prepare.add_argument("image", type=pathlib.Path)
    prepare.add_argument("-o", "--output-dir", type=pathlib.Path, default=pathlib.Path("tftp-root"))

    tftp = subparsers.add_parser("tftp", help="serve tftp-root/recovery.bin for U-Boot recovery")
    tftp.add_argument("--host", default="0.0.0.0")
    tftp.add_argument("--port", type=int, default=69)
    tftp.add_argument("--directory", type=pathlib.Path, default=pathlib.Path("tftp-root"))

    web = subparsers.add_parser("web", help="start the localhost browser interface")
    web.add_argument("--host", default=WEB_HOST)
    web.add_argument("--port", type=int, default=WEB_PORT)

    upgrade = subparsers.add_parser("sysupgrade", help="upload an image to a running OpenWrt router")
    upgrade.add_argument("image", type=pathlib.Path)
    upgrade.add_argument("--host", default="192.168.1.1")
    upgrade.add_argument("--user", default="root")
    upgrade.add_argument("--port", type=int, default=22)
    upgrade.add_argument("--keep-settings", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "download":
            download_verified(image_info(args.release, args.kind), args.output_dir)
        elif args.command == "prepare-recovery":
            prepare_recovery(args.image, args.output_dir)
        elif args.command == "tftp":
            serve_tftp(args.directory, args.host, args.port)
        elif args.command == "web":
            run_web(args.host, args.port)
        elif args.command == "sysupgrade":
            ssh_upgrade(args.host, args.user, args.image, args.port, args.keep_settings)
        return 0
    except (FlasherError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
