#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TMDB 取数通道（只读）—— MediaNomenclator 的数据源适配器

用途
----
替代「WebFetch 抓 TMDB 网页」，用官方 API v3 精确取：
  · 作品身份（剧名 / 原名 / 首播年 / 国家）
  · 季结构（季号 / 季名 / 该季首播年 / 集数）—— 季集判定的硬性依据
  · 分集标题（zh-CN 优先，自动识别「第N集」占位编号）
  · 别名与各地区译名 —— 「中文译名须多方查证」的其中一源

设计要点
--------
1. **只读**：只发 GET，绝不写任何远端数据。
2. **令牌不外泄**：从环境变量 / 独立密钥文件读取，绝不打进日志或聊天。
   来源优先级： 环境变量 TMDB_API_KEY > ~/.config/media-nomenclator/tmdb_api_key > 兼容路径
3. **DNS 污染绕行**：当系统 DNS 把 api.themoviedb.org 解析成错误 IP 时直连必超时。
   做法：先用 DoH（doh.pub 等）取真实 IP，再用「固定 IP + 正确 SNI」建 TLS，
   从而既绕过污染又保持证书校验（不关 verify）。
   结果按 TTL 缓存到 ~/.cache/media-nomenclator/tmdb_dns.json。
4. **占位编号自动标注**：`第1集` / `Episode 1` 之类不算集标题，脚本标 placeholder。
   注意：纯数字（如 `737`、`51`）是**真实**集标题，不标 placeholder。

用法
----
  PY=python3
  $PY tmdb_api.py selftest                       # 自检（连不通/密钥失效会明确报错）
  $PY tmdb_api.py search tv "剧名"                # 搜剧，看候选条目
  $PY tmdb_api.py search movie "片名" --year 2010
  $PY tmdb_api.py tv 203737                      # 剧详情 + 季结构表
  $PY tmdb_api.py season 203737 1                # 第1季全部集标题（中文+英文对照）
  $PY tmdb_api.py movie 27205                    # 电影详情（年份/时长）
  $PY tmdb_api.py titles tv 203737               # 别名 / 各地区译名
  $PY tmdb_api.py raw /tv/203737 --json          # 任意端点原样输出
  常用开关： --json（机器可读）  --lang zh-CN  --no-pin（禁用 DoH 绕行）  -v（略详细）
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "api.themoviedb.org"
API_BASE = "/3"
UA = "MediaNomenclator/0.1 (media library metadata & naming toolkit)"

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get(
    "MEDIA_NOMENCLATOR_HOME", os.path.join(HOME, ".config", "media-nomenclator"))
KEY_FILES = [
    os.path.join(CONFIG_DIR, "tmdb_api_key"),
    os.path.join(CONFIG_DIR, "tmdb_token"),
]
CACHE_DIR = os.environ.get(
    "MEDIA_NOMENCLATOR_CACHE", os.path.join(HOME, ".cache", "media-nomenclator"))
DNS_CACHE = os.path.join(CACHE_DIR, "tmdb_dns.json")
DNS_TTL = 3600
DOH_ENDPOINTS = [
    "https://doh.pub/dns-query?name={name}&type=A",
    "https://doh.360.cn/resolve?name={name}&type=A",
    "https://dns.alidns.com/resolve?name={name}&type=A",
]

# 「假集标题」判定（§1.3）—— 占位编号不算标题；纯数字不算假（「737」是真实集标题）
PLACEHOLDER_RES = [
    re.compile(r"^第\s*\d+\s*[集话回]$"),
    re.compile(r"^第\s*[一二三四五六七八九十百零两]+\s*[集话回]$"),
    re.compile(r"^Episode\s*0*\d+$", re.I),
    re.compile(r"^EP\.?\s*0*\d+$", re.I),
    re.compile(r"^Ep\.?\s*0*\d+$"),
]


class TmdbError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def load_key() -> str:
    env = (os.environ.get("TMDB_API_KEY") or "").strip()
    if env:
        return env
    for p in KEY_FILES:
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            continue
    raise TmdbError(
        "找不到 TMDB API 密钥。请二选一：\n"
        "  ① 把密钥写入 " + KEY_FILES[0] + "\n"
        "  ② 设置环境变量 TMDB_API_KEY"
    )


def mask(key: str) -> str:
    return key[:4] + "*" * 8 + key[-4:] if len(key) > 10 else "****"


# --------------------------------------------------------------------------- #
# 网络：DoH 解析 + 固定 IP 直连
# --------------------------------------------------------------------------- #
def _openers(prefer_direct: bool = True):
    """返回可依次尝试的 opener 列表：先不走代理，再走环境代理。"""
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    proxied = urllib.request.build_opener()
    return [direct, proxied] if prefer_direct else [proxied, direct]


def _http_get(url: str, timeout: float = 10.0, accept: str | None = None) -> bytes:
    last = None
    for op in _openers():
        try:
            headers = {"User-Agent": UA}
            if accept:
                headers["accept"] = accept
            req = urllib.request.Request(url, headers=headers)
            with op.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
    raise TmdbError(f"HTTP 取数失败：{url} → {type(last).__name__}: {last}")


def _read_cache() -> dict:
    try:
        with open(DNS_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(d: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(DNS_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def system_dns_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        return list(dict.fromkeys(i[4][0] for i in infos))
    except Exception:  # noqa: BLE001
        return []


def doh_ips(host: str, verbose: bool = False) -> list[str]:
    out: list[str] = []
    for tpl in DOH_ENDPOINTS:
        url = tpl.format(name=urllib.parse.quote(host))
        try:
            raw = _http_get(url, timeout=8, accept="application/dns-json")
            data = json.loads(raw.decode("utf-8"))
            got = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1 and a.get("data")]
            if verbose and got:
                print(f"  [doh] {url.split('/')[2]} → {got}", file=sys.stderr)
            out.extend(got)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  [doh] {url.split('/')[2]} 失败：{type(e).__name__}", file=sys.stderr)
    return list(dict.fromkeys(out))


def candidate_ips(host: str = API_HOST, use_doh: bool = True, verbose: bool = False) -> list[str]:
    """按「DoH → 缓存 → 系统 DNS」汇总候选 IP，去重后返回（顺序即优先级）。"""
    cands: list[str] = []
    cache = _read_cache()
    entry = cache.get(host) or {}
    fresh = (time.time() - float(entry.get("ts") or 0)) < DNS_TTL

    if fresh and entry.get("ips"):
        cands.extend(entry["ips"])
    if use_doh:
        cands.extend(doh_ips(host, verbose=verbose))
    cands.extend(system_dns_ips(host))

    seen, out = set(), []
    for ip in cands:
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)

    # 固定 IP 直连成功后再回写缓存（由 request_json 调用 confirm_cache 完成）
    return out


def confirm_cache(host: str, ip: str) -> None:
    cache = _read_cache()
    entry = cache.get(host) or {}
    ips = [ip] + [x for x in (entry.get("ips") or []) if x != ip]
    cache[host] = {"ts": time.time(), "ips": ips[:6]}
    _write_cache(cache)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到指定 IP，但 SNI/Host 仍是域名 —— 证书照常校验，只绕开被污染的 DNS。"""

    def __init__(self, host: str, ip: str, **kw):
        super().__init__(host, **kw)
        self._pin_ip = ip

    def connect(self) -> None:  # type: ignore[override]
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        ctx = self._context or ssl.create_default_context()
        self.sock = ctx.wrap_socket(sock, server_hostname=self.host)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def request_json(path: str, params: dict | None = None, *, use_doh: bool = True,
                 verbose: bool = False, timeout: float = 20.0) -> dict:
    key = load_key()
    q = dict(params or {})
    q["api_key"] = key
    qs = urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    full = f"{API_BASE}{path}?{qs}"

    errors: list[str] = []
    for ip in candidate_ips(API_HOST, use_doh=use_doh, verbose=verbose):
        try:
            conn = PinnedHTTPSConnection(API_HOST, ip, timeout=timeout)
            conn.request("GET", full, headers={"Host": API_HOST, "User-Agent": UA,
                                               "Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status >= 400:
                errors.append(f"{ip} → HTTP {resp.status}")
                # 400/401/404 是确定性的业务错误，换 IP 无用 → 直接抛出
                if resp.status in (400, 401, 403, 404, 422):
                    raise TmdbError(_explain_http(resp.status, body))
                continue
            confirm_cache(API_HOST, ip)
            return json.loads(body.decode("utf-8"))
        except TmdbError:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ip} → {type(e).__name__}: {str(e)[:60]}")

    # 候选 IP 全败 → 退回系统 DNS 直连（部分环境污染不影响 TLS）
    try:
        conn = http.client.HTTPSConnection(API_HOST, timeout=timeout)
        conn.request("GET", full, headers={"User-Agent": UA, "Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status >= 400:
            raise TmdbError(_explain_http(resp.status, body))
        return json.loads(body.decode("utf-8"))
    except TmdbError:
        raise
    except Exception as e:  # noqa: BLE001
        errors.append(f"系统 DNS 直连 → {type(e).__name__}: {str(e)[:60]}")

    raise TmdbError(
        "TMDB API 不可达。已尝试：\n  " + "\n  ".join(errors) +
        "\n提示：本机 api.themoviedb.org 有 DNS 污染，脚本默认走 DoH 固定 IP；"
        "若 DoH 也被拦，可检查网络或改用 §13 Step 4 的网页抓取兜底。"
    )


def _explain_http(status: int, body: bytes) -> str:
    msg = ""
    try:
        msg = (json.loads(body.decode("utf-8")) or {}).get("status_message", "")
    except Exception:  # noqa: BLE001
        pass
    hint = {
        401: "密钥无效或未启用（去 TMDB 账号设置确认 API Key）",
        403: "密钥被拒（可能已吊销）",
        404: "条目不存在（id 写错？）",
        422: "参数非法",
    }.get(status, "")
    return f"TMDB 返回 HTTP {status}：{msg or '无详情'}" + (f"｜{hint}" if hint else "")


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def is_placeholder(title: str | None) -> bool:
    if not title or not title.strip():
        return True
    t = title.strip()
    return any(r.match(t) for r in PLACEHOLDER_RES)


def mark(title: str | None, lang_note: str = "") -> str:
    if not title or not title.strip():
        return "（无）"
    if is_placeholder(title):
        return f"[占位:{title}]"
    return title


def pick_name(obj: dict) -> str:
    return obj.get("name") or obj.get("title") or ""


def pick_original(obj: dict) -> str:
    return obj.get("original_name") or obj.get("original_title") or ""


def pick_date(obj: dict) -> str:
    return obj.get("first_air_date") or obj.get("release_date") or ""


def year_of(obj: dict) -> str:
    d = pick_date(obj)
    return d[:4] if d and len(d) >= 4 else ""


def fmt_runtime(mins) -> str:
    if not mins:
        return ""
    mins = int(mins)
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def hr(ch: str = "─", n: int = 78) -> str:
    print(ch * n)


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_search(a) -> int:
    kind = "tv" if a.kind == "tv" else "movie"
    data = request_json(f"/search/{kind}", {"query": a.query, "language": a.lang,
                                            "include_adult": "false",
                                            **({"year": a.year} if a.year else {})},
                        use_doh=not a.no_pin, verbose=a.verbose)
    rows = (data.get("results") or [])[: a.limit]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"没有搜到「{a.query}」。换个关键词，或先用英文原名搜。")
        return 1
    hr()
    print(f"「{a.query}」的候选条目（共 {data.get('total_results', len(rows))} 条，显示前 {len(rows)}）")
    hr()
    for r in rows:
        rid = r.get("id")
        name = pick_name(r)
        orig = pick_original(r)
        y = year_of(r)
        cc = ",".join(r.get("origin_country") or []) or (r.get("original_language") or "")
        # 注：search 端点不返回季数/集数，故此处不展示（避免出现 "? 季" 这种噪音）
        extra = f"{pick_date(r)}" if kind == "tv" else f"{fmt_runtime(r.get('runtime'))}"
        print(f"  id={rid:<8} {y:<5} [{cc}] {name}")
        if orig and orig != name:
            print(f"           原名: {orig}")
        extra = extra.strip(", ")
        if extra:
            print(f"           {extra}")
        ov = (r.get("overview") or "").strip().replace("\n", " ")
        if ov:
            print(f"           {(ov[:78] + '…') if len(ov) > 78 else ov}")
    hr()
    print("确认条目无误后，用它的 id 继续： tmdb_api.py tv <id>   /   movie <id>")
    return 0


def cmd_tv(a) -> int:
    d = request_json(f"/tv/{a.id}", {"language": a.lang}, use_doh=not a.no_pin, verbose=a.verbose)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    hr()
    print(f"TMDB id {d.get('id')}｜{pick_name(d)}")
    hr()
    print(f"  原名      : {pick_original(d)}")
    print(f"  首播年    : {year_of(d)}   首播日: {pick_date(d)}")
    print(f"  产地/语言 : {','.join(d.get('origin_country') or [])} / {d.get('original_language')}")
    print(f"  状态      : {d.get('status')}   类型: {','.join(g['name'] for g in d.get('genres') or [])}")
    print(f"  总季数    : {d.get('number_of_seasons')}   总集数: {d.get('number_of_episodes')}")
    ov = (d.get("overview") or "").strip()
    if ov:
        print(f"  简介      : {ov[:160]}{'…' if len(ov) > 160 else ''}")

    seasons = d.get("seasons") or []
    if seasons:
        hr()
        print(f"{'季号':<5}{'季名(TMDB)':<28}{'首播年':<8}{'集数':<6}首播日")
        hr()
        for s in seasons:
            sn = s.get("season_number")
            ad = s.get("air_date") or ""
            ec = s.get("episode_count") or 0
            ecell = str(ec) if ec else "—"
            flag = "" if ec else "   ← 尚未播出/无分集"
            print(f"S{sn:<4}{s.get('name', ''):<28}{ad[:4]:<8}{ecell:<6}{ad or '—'}{flag}")
        hr()
        print("★ 硬性检查：文件名里写的季号，必须出现在上表（否则刮削必失败）。")
        print("★ 多季/续集：剧名取「该季的季名」，年份取「该季首播年」，季号不重置（SKILL §1.2.1）。")
        print("★ 若某季季名在 TMDB 上只是短名（如「西行」），需补系列前缀成正式片名。")
        print("★ 下一句可看集标题： tmdb_api.py season <id> <季号>")
    return 0


def cmd_season(a) -> int:
    d = request_json(f"/tv/{a.id}/season/{a.season}", {"language": a.lang},
                     use_doh=not a.no_pin, verbose=a.verbose)
    eps = d.get("episodes") or []
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    # 额外取英文，便于「中文无可靠标题 → 用英文」这条链（§1.3）
    en = {}
    if not a.lang_only and a.lang != "en-US":
        try:
            de = request_json(f"/tv/{a.id}/season/{a.season}", {"language": "en-US"},
                              use_doh=not a.no_pin, verbose=False)
            en = {e.get("episode_number"): (e.get("name") or "") for e in de.get("episodes") or []}
        except TmdbError:
            en = {}

    hr()
    sname = (d.get("name") or "").strip()
    if is_placeholder(sname) or sname in (f"第 {a.season} 季", f"第{a.season}季", ""):
        sname = f"第 {a.season} 季"
    print(f"「{sname}」｜共 {len(eps)} 集｜首播 {d.get('air_date') or '未知'}｜语言 {a.lang}")
    hr()
    if not eps:
        print("（该季没有分集数据）")
        return 1
    print(f"{'集':<6}{'中文集标题':<30}{'英文集标题':<28}首播日")
    hr()
    real_zh = placeholder_zh = none_zh = 0
    for e in eps:
        n = e.get("episode_number")
        zh = (e.get("name") or "").strip()
        en_t = (en.get(n) or "").strip()
        if not zh:
            none_zh += 1
        elif is_placeholder(zh):
            placeholder_zh += 1
        else:
            real_zh += 1
        print(f"E{n:<5}{mark(zh):<30}{mark(en_t):<28}{e.get('air_date') or ''}")
    hr()
    print(f"统计：真实中文集标题 {real_zh} 条；中文占位编号 {placeholder_zh} 条；中文缺失 {none_zh} 条"
          f"（共 {len(eps)} 集）")
    print("★ 只有「真实标题」才能写进文件名；占位编号与缺失一律按 §1.3 处理"
          "（中文无 → 看英文 → 都无 → 省略该段）。")
    print("★ 禁止机翻、禁止自编集标题。")
    return 0


def cmd_movie(a) -> int:
    d = request_json(f"/movie/{a.id}", {"language": a.lang}, use_doh=not a.no_pin, verbose=a.verbose)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    hr()
    print(f"TMDB id {d.get('id')}｜{pick_name(d)}")
    hr()
    print(f"  原名    : {pick_original(d)}")
    print(f"  上映年  : {year_of(d)}   上映日: {pick_date(d)}")
    print(f"  时长    : {fmt_runtime(d.get('runtime'))}   状态: {d.get('status')}")
    print(f"  产地    : {','.join(d.get('production_countries') and [c.get('name') for c in d['production_countries']] or [])}")
    print(f"  类型    : {','.join(g['name'] for g in d.get('genres') or [])}")
    ov = (d.get("overview") or "").strip()
    if ov:
        print(f"  简介    : {ov[:160]}{'…' if len(ov) > 160 else ''}")
    hr()
    print("★ 电影文件名格式（规则 v2）： 片名 (年份) 分辨率.ext")
    print("★ 分辨率不在 TMDB 里，必须读本地 MediaInfo；读不到 → NEED_REVIEW，不猜。")
    return 0


def cmd_titles(a) -> int:
    kind = "tv" if a.kind == "tv" else "movie"
    lang_param = "name" if kind == "tv" else "title"
    out: dict = {"id": a.id, "kind": kind, "translations": [], "alternative_titles": []}

    for lang in ("zh-CN", "zh-TW", "zh-HK", "zh-SG"):
        try:
            d = request_json(f"/{kind}/{a.id}", {"language": lang}, use_doh=not a.no_pin)
            nm = pick_name(d)
            if nm:
                out["translations"].append({"lang": lang, "name": nm})
        except TmdbError:
            continue
    try:
        d = request_json(f"/{kind}/{a.id}/alternative_titles", {}, use_doh=not a.no_pin)
        seen_t = set()
        for t in d.get("results") or []:
            iso = (t.get("iso_3166_1") or "").upper()
            nm = (t.get(lang_param) or t.get("title") or "").strip()
            if not nm or nm in seen_t:
                continue
            # 只要「中国相关地区」或「含中日韩文字」的别名（其余多为拉丁字母转写，无用）
            if iso in ("CN", "TW", "HK", "SG") or re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", nm):
                seen_t.add(nm)
                out["alternative_titles"].append({"region": iso, "title": nm})
        out["alternative_titles"] = out["alternative_titles"][:40]
    except TmdbError:
        pass

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    hr()
    print(f"TMDB id {a.id}（{kind}）的地区译名与别名")
    hr()
    print("【按 language 参数取到的名称】")
    for t in out["translations"]:
        print(f"  {t['lang']:<7} {t['name']}")
    print("\n【alternative_titles 中与中国相关的条目】")
    if out["alternative_titles"]:
        for t in out["alternative_titles"]:
            print(f"  {t['region'] or '(无地区)':<9} {t['title']}")
    else:
        print("  （无）")
    hr()
    print("★ 注意：以上全部来自 TMDB —— 按 §1.2 只算「一源」。")
    print("  非官方来源需 ≥2 个**独立**来源一致才能定中文名，"
          "zh-CN 与 zh-TW 同属 TMDB，不能自相印证。")
    print("  仍需去豆瓣 / 维基中文 / 官方平台交叉验证。")
    return 0


def cmd_raw(a) -> int:
    params = {}
    for kv in a.param or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v
    params.setdefault("language", a.lang)
    d = request_json(a.path, params, use_doh=not a.no_pin, verbose=a.verbose)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def cmd_selftest(a) -> int:
    TOTAL = 6
    ok = 0
    hr("=")
    print("TMDB 通道自检（只读，不改任何远端数据）")
    hr("=")

    # 1 密钥
    try:
        k = load_key()
        print(f"[1/{TOTAL}] 密钥        OK（{mask(k)}，来源可用）")
        ok += 1
    except TmdbError as e:
        print(f"[1/{TOTAL}] 密钥        FAIL：{e}")
        hr("=")
        return 1

    # 2 DoH
    ips = doh_ips(API_HOST, verbose=True) if not a.no_pin else []
    if ips:
        print(f"[2/{TOTAL}] DoH 解析    OK（{', '.join(ips[:4])}）")
        ok += 1
    else:
        print(f"[2/{TOTAL}] DoH 解析    WARN：无结果（将退回系统 DNS）")

    # 3 连通
    try:
        d = request_json("/configuration", {}, use_doh=not a.no_pin)
        base = (d.get("images") or {}).get("secure_base_url")
        print(f"[3/{TOTAL}] API 连通    OK（images.secure_base_url = {base}）")
        ok += 1
    except TmdbError as e:
        print(f"[3/{TOTAL}] API 连通    FAIL：{e}")
        hr("=")
        return 1

    # 4 真实业务查询：校验「按 id 取季结构」这条链路（条目 id 可用环境变量替换）
    probe_id = os.environ.get("TMDB_SELFTEST_ID", "203737")
    try:
        d = request_json(f"/tv/{probe_id}", {"language": "zh-CN"}, use_doh=not a.no_pin)
        s1 = [s for s in d.get("seasons") or [] if s.get("season_number") == 1]
        n1 = s1[0].get("episode_count") if s1 else None
        if pick_name(d) and n1:
            print(f"[4/{TOTAL}] 业务查询    OK（id {probe_id} = {pick_name(d)}，"
                  f"{d.get('number_of_seasons')} 季 / S1 {n1} 集）")
            ok += 1
        else:
            print(f"[4/{TOTAL}] 业务查询    WARN：{pick_name(d)}｜季数 "
                  f"{d.get('number_of_seasons')}｜S1 集数 {n1}")
            ok += 1
    except TmdbError as e:
        print(f"[4/{TOTAL}] 业务查询    FAIL：{e}")
        hr("=")
        return 1

    # 5 分集标题 + 占位识别
    try:
        d = request_json(f"/tv/{probe_id}/season/1", {"language": "zh-CN"},
                         use_doh=not a.no_pin)
        eps = d.get("episodes") or []
        e1 = next((e for e in eps if e.get("episode_number") == 1), {})
        nm = e1.get("name") or ""
        if nm and not is_placeholder(nm):
            print(f"[5/{TOTAL}] 集标题      OK（E01 = {nm}，真实标题）")
            ok += 1
        else:
            print(f"[5/{TOTAL}] 集标题      WARN：E01 = {nm!r}（被判定为占位/缺失）")
            ok += 1
    except TmdbError as e:
        print(f"[5/{TOTAL}] 集标题      FAIL：{e}")
        hr("=")
        return 1

    # 6 占位识别逻辑（离线）
    cases = [("第1集", True), ("第 12 话", True), ("Episode 3", True), ("EP05", True),
             ("示例集标题", False), ("737", False), ("51", False), ("恶魔 79", False), ("", True)]
    bad = [c for c, exp in cases if is_placeholder(c) != exp]
    if not bad:
        print(f"[6/{TOTAL}] 占位识别    OK（9 条用例全对；纯数字视为真实标题）")
        ok += 1
    else:
        print(f"[6/{TOTAL}] 占位识别    FAIL：误判 {bad}")

    hr("=")
    print(f"结果：{ok}/{TOTAL} 通过（WARN 亦计入通过）")
    hr("=")
    if ok == TOTAL:
        print("通道可用。可直接用：tv / season / movie / search / titles")
    return 0 if ok == TOTAL else 1


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tmdb_api.py",
        description="TMDB 取数通道（只读）—— 供 media-library-auto-rename 使用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[1] if "用法" in __doc__ else "",
    )
    p.add_argument("--json", action="store_true", help="输出原始 JSON（机器可读）")
    p.add_argument("--lang", default="zh-CN", help="语言参数，默认 zh-CN")
    p.add_argument("--no-pin", action="store_true", help="禁用 DoH 固定 IP（改用系统 DNS）")
    p.add_argument("-v", "--verbose", action="store_true", help="打印 DoH 解析细节")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("selftest", help="自检：密钥 / DoH / 连通 / 业务查询 / 占位识别")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("search", help="搜索剧集或电影，拿候选 id")
    s.add_argument("kind", choices=["tv", "movie"])
    s.add_argument("query")
    s.add_argument("--year", help="限定年份")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("tv", help="剧详情 + 季结构表（季号/季名/该季首播年/集数）")
    s.add_argument("id")
    s.set_defaults(func=cmd_tv)

    s = sub.add_parser("season", help="某季全部集标题（中文 + 英文对照，自动标注占位编号）")
    s.add_argument("id")
    s.add_argument("season")
    s.add_argument("--lang-only", action="store_true", help="只看 --lang，不取英文")
    s.set_defaults(func=cmd_season)

    s = sub.add_parser("movie", help="电影详情（原名/年份/时长）")
    s.add_argument("id")
    s.set_defaults(func=cmd_movie)

    s = sub.add_parser("titles", help="地区译名与别名（供中文译名多方查证参考）")
    s.add_argument("kind", choices=["tv", "movie"])
    s.add_argument("id")
    s.set_defaults(func=cmd_titles)

    s = sub.add_parser("raw", help="直接打任意端点，原样输出 JSON")
    s.add_argument("path", help="如 /tv/203737 或 /search/multi")
    s.add_argument("param", nargs="*", help="附加参数 k=v")
    s.set_defaults(func=cmd_raw)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TmdbError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
