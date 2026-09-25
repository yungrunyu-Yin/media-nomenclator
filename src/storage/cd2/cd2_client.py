"""
CloudDrive2 gRPC 客户端（影视媒体库重命名专用，最小实现）

职责：只做 5 类文件操作 —— 列目录 / 读文件信息 / 建目录 / rename / move。
明确不做：上传、下载、删除、读写文件内容。

认证：gRPC metadata  `authorization: Bearer <API_TOKEN>`（proto 第 43 行注明）。
  令牌来源优先级：环境变量 CD2_API_TOKEN > 令牌文件 cd2/token.txt > ~/.config/media-nomenclator/cd2_token
  令牌**永不**打印、永不写入任何日志或输出。

端口：自动探测 19798（core 默认）→ 29798（WinUI 版实测）。
"""

from __future__ import annotations

import os
import sys
import grpc

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_GEN = os.path.join(_HERE, "gen")
if _GEN not in sys.path:
    sys.path.insert(0, _GEN)

import clouddrive_pb2 as pb  # noqa: E402
import clouddrive_pb2_grpc as pb_grpc  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORTS = (19798, 29798)

TOKEN_FILES = (
    os.path.join(_HERE, "token.txt"),
    os.path.join(os.path.expanduser("~"), ".config", "media-nomenclator", "cd2_token"),
)

# CloudDrive2 的配置目录候选（WinUI 版实测为第一个）
CONFIG_DIRS = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "CloudDrive.WinUI"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "CloudDrive"),
    os.path.join(os.environ.get("APPDATA", ""), "CloudDrive"),
)


def _ports_from_config() -> list[int]:
    """从 CloudDrive2 自己的 config.toml 读 [webconfig] 端口。

    比写死端口可靠：CD2 升级或用户改配置后仍能连上。
    读不到就返回空列表，由调用方回退到 DEFAULT_PORTS。
    """
    out: list[int] = []
    for d in CONFIG_DIRS:
        p = os.path.join(d, "config.toml")
        if not d or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
            try:
                import tomllib  # Python >= 3.11
                cfg = tomllib.loads(raw.decode("utf-8", "replace"))
                wc = cfg.get("webconfig", {})
                if wc.get("enable_https") and wc.get("https_port"):
                    out.append(int(wc["https_port"]))
                for k in ("http_port",):
                    if wc.get(k):
                        out.append(int(wc[k]))
            except Exception:  # noqa: BLE001
                import re
                for m in re.finditer(r"^\s*(https?)_port\s*=\s*(\d+)", raw.decode("utf-8", "replace"), re.M):
                    out.append(int(m.group(2)))
        except OSError:
            continue
        if out:
            break
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


class CD2Error(RuntimeError):
    pass


def _load_token() -> str:
    tok = os.environ.get("CD2_API_TOKEN", "").strip()
    if tok:
        return tok
    for p in TOKEN_FILES:
        try:
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    tok = fh.read().strip()
                if tok:
                    return tok
        except OSError:
            pass
    raise CD2Error(
        "未找到 CloudDrive2 API 令牌。请设置环境变量 CD2_API_TOKEN，"
        f"或把令牌写入 {TOKEN_FILES[0]}（该文件不会被打印或上传）。"
    )


def redact(text: str) -> str:
    """把输出里可能出现的令牌串抹掉（防御性，令牌本身从不主动打印）。"""
    tok = os.environ.get("CD2_API_TOKEN", "").strip()
    if tok:
        text = text.replace(tok, "***REDACTED***")
    for p in TOKEN_FILES:
        try:
            if os.path.isfile(p):
                t = open(p, encoding="utf-8").read().strip()
                if t:
                    text = text.replace(t, "***REDACTED***")
        except OSError:
            pass
    return text


class CD2Client:
    def __init__(self, host: str | None = None, port: int | None = None,
                 token: str | None = None, timeout: float = 60.0):
        self.host = host or os.environ.get("CD2_HOST", DEFAULT_HOST)
        self.timeout = timeout
        self._token = token or _load_token()
        self._md = (("authorization", f"Bearer {self._token}"),)

        ports = (port,) if port else tuple(
            int(p) for p in os.environ.get("CD2_PORTS", "").split(",") if p.strip()
        ) or tuple(_ports_from_config()) or DEFAULT_PORTS
        self.port = None
        self.chan = None
        last_err = None
        # gRPC 必须绕过系统 HTTP 代理（很多环境默认设了 http_proxy，会让握手 502）
        opts = [
            ("grpc.enable_http_proxy", 0),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
        ]
        for p in ports:
            ch = grpc.insecure_channel(f"{self.host}:{p}", options=opts)
            try:
                grpc.channel_ready_future(ch).result(timeout=4)
                stub = pb_grpc.CloudDriveFileSrvStub(ch)
                # GetSystemInfo 是公开方法，用它验证端口确实是 CloudDrive2
                stub.GetSystemInfo(pb.google_dot_protobuf_dot_empty__pb2.Empty(),
                                   timeout=8)
                self.port, self.chan = p, ch
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                ch.close()
        if self.chan is None:
            raise CD2Error(
                f"无法连接 CloudDrive2 gRPC（尝试端口 {ports}）：{redact(str(last_err))}"
            )
        self.stub = pb_grpc.CloudDriveFileSrvStub(self.chan)

    # ---------------- 只读 ----------------

    def system_info(self) -> dict:
        r = self.stub.GetSystemInfo(pb.google_dot_protobuf_dot_empty__pb2.Empty(),
                                    timeout=self.timeout)
        return {f.name: getattr(r, f.name) for f in r.DESCRIPTOR.fields
                if f.name in ("IsLogin", "UserName", "SystemReady", "SystemMessage")}

    def list_dir(self, path: str, force_refresh: bool = True) -> list[dict]:
        """列目录。CD2 有目录缓存，校验时必须 force_refresh=True。"""
        req = pb.ListSubFileRequest(path=path, forceRefresh=force_refresh)
        out: list[dict] = []
        for rep in self.stub.GetSubFiles(req, timeout=self.timeout, metadata=self._md):
            for f in rep.subFiles:
                out.append({
                    "name": f.name,
                    "path": f.fullPathName,
                    "isDir": f.isDirectory,
                    "size": f.size,
                })
        return out

    def stat(self, parent_path: str, name: str) -> dict | None:
        """取单个条目信息；不存在返回 None（不抛异常）。"""
        req = pb.FindFileByPathRequest(parentPath=parent_path, path=name)
        try:
            f = self.stub.FindFileByPath(req, timeout=self.timeout, metadata=self._md)
        except grpc.RpcError:
            return None
        if not f.name:
            return None
        return {"name": f.name, "path": f.fullPathName, "isDir": f.isDirectory,
                "size": f.size}

    def exists(self, parent_path: str, name: str) -> bool:
        return self.stat(parent_path, name) is not None

    # ---------------- 写操作（仅 mkdir / rename / move） ----------------

    def mkdir(self, parent_path: str, folder_name: str) -> dict:
        req = pb.CreateFolderRequest(parentPath=parent_path, folderName=folder_name)
        r = self.stub.CreateFolder(req, timeout=self.timeout, metadata=self._md)
        if not r.result.success:
            raise CD2Error(f"mkdir 失败 {parent_path}/{folder_name}: {r.result.errorMessage}")
        return {"path": r.folderCreated.fullPathName}

    def rename(self, the_file_path: str, new_name: str) -> dict:
        req = pb.RenameFileRequest(theFilePath=the_file_path, newName=new_name)
        r = self.stub.RenameFile(req, timeout=self.timeout, metadata=self._md)
        if not r.success:
            raise CD2Error(f"rename 失败 {the_file_path} -> {new_name}: {r.errorMessage}")
        return {"from": the_file_path, "to": new_name,
                "reported": list(r.resultFilePaths)}

    def move(self, file_paths: list[str], dest_path: str,
             conflict_policy: str = "skip") -> dict:
        """conflict_policy: overwrite / rename / skip —— 本项目**固定用 skip**，
        绝不覆盖同名文件（安全底线）。"""
        pol = {"overwrite": 0, "rename": 1, "skip": 2}[conflict_policy]
        req = pb.MoveFileRequest(theFilePaths=file_paths, destPath=dest_path,
                                 conflictPolicy=pol)
        r = self.stub.MoveFile(req, timeout=self.timeout, metadata=self._md)
        if not r.success:
            raise CD2Error(f"move 失败 {file_paths} -> {dest_path}: {r.errorMessage}")
        return {"from": list(file_paths), "dest": dest_path,
                "reported": list(r.resultFilePaths)}

    def close(self):
        if self.chan is not None:
            self.chan.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
