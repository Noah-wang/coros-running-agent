"""B 站字幕抓取和 UP 主视频列表。

原来是 `subprocess` 调一个叫 `bilibili-subtitle-fetch` 的外部 CLI。
那个 CLI **在服务器上根本不存在**——`running-video` 一调就
`FileNotFoundError`，而这条路一直没人触发过，所以坏了很久也没发现。

现在改成直接调 B 站接口。三个好处：

- **少一层进程。** 抓字幕是用户等着的同步调用，`subprocess` 的启动开销
  和「二进制装没装」的不确定性都落在体感上。
- **返回值是结构化的。** 原来只能拿到 CLI 打印的文本。
- **凭据只有一份。** 继续用已有的 `config.toml`，不再多维护一个 json。

WBI 签名交给 `bilibili-api-python` 维护。这是整条链上唯一依赖未公开接口的
地方，B 站会不定期改，让一个持续更新的库去跟比自己写划算——
同样的理由让我们没有自建 RSSHub：那要多养一个常驻容器，
而这里只是一个 pip 包。
"""

import asyncio
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import httpx
from bilibili_api import Credential, user

from src.runtime.paths import ROOT_DIR
from src.runtime.trace import log_event

DEFAULT_CONFIG_PATHS = (
    ROOT_DIR / "agents" / "coros_report" / "config.toml",
    Path.home() / ".config" / "bilibili-subtitle-fetch" / "config.toml",
)

TIMEOUT_SECONDS = 20
# 翻页和逐条抓取之间的停顿。参考实现用 1~2 秒，触发 412 风控就停。
# 这不是礼貌问题——风控一旦触发，整个账号的接口会短时间不可用。
PAGE_PAUSE_SECONDS = 1.0
VIDEO_PAUSE_SECONDS = 2.0

BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{8,}")


def bilibili_config_path() -> Path:
    configured = os.getenv("BILIBILI_SUBTITLE_CONFIG")
    if configured:
        return Path(configured).expanduser()

    for path in DEFAULT_CONFIG_PATHS:
        if path.exists():
            return path

    return DEFAULT_CONFIG_PATHS[0]


def _credential() -> Credential:
    path = bilibili_config_path()
    if not path.exists():
        raise RuntimeError(f"Bilibili config not found: {path}")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    section = data.get("credential", data)
    sessdata = str(section.get("sessdata", ""))
    if not sessdata:
        raise RuntimeError(f"Bilibili config has no sessdata: {path}")

    return Credential(
        sessdata=sessdata,
        bili_jct=str(section.get("bili_jct", "")),
        buvid3=str(section.get("buvid", "") or section.get("buvid3", "")),
    )


def _headers(credential: Credential) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com",
        "Cookie": (
            f"SESSDATA={credential.sessdata}; "
            f"BILI_JCT={credential.bili_jct}; "
            f"buvid3={credential.buvid3}"
        ),
    }


def extract_bvid(video_input: str) -> str:
    """从链接或裸 BV 号里取出 BV。取不到就原样返回，让上层报错。"""
    match = BV_PATTERN.search(video_input)
    return match.group(0) if match else video_input.strip()


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict) -> dict[str, Any]:
    response = await client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


async def _subtitle_tracks(
    client: httpx.AsyncClient, aid: int, cid: int, bvid: str, headers: dict
) -> list[dict[str, Any]]:
    """取字幕轨。主接口拿不到就退回旧接口。

    两个接口都要试：wbi/v2 是当前的，player/v2 是旧的但有时反而有数据。
    """
    primary = await _get_json(
        client,
        f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}&bvid={bvid}",
        headers,
    )
    if primary.get("code") == 0:
        tracks = primary.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if tracks:
            return tracks

    fallback = await _get_json(
        client, f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}", headers
    )
    if fallback.get("code") == 0:
        return fallback.get("data", {}).get("subtitle", {}).get("subtitles", [])
    return []


def _chinese_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只留中文轨，包括 AI 生成的。

    AI 字幕的 lan 是 ai-zh，lan_doc 不一定含「中文」，所以两个条件都要判。
    """
    return [
        track
        for track in tracks
        if "中文" in str(track.get("lan_doc", ""))
        or str(track.get("lan", "")).startswith("ai-zh")
    ]


async def fetch_bilibili_subtitle(video_input: str) -> str:
    """抓一条视频的中文字幕，返回纯文本（一行一句）。"""
    bvid = extract_bvid(video_input)
    credential = _credential()
    headers = _headers(credential)

    async with httpx.AsyncClient() as client:
        info = await _get_json(
            client, f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", headers
        )
        if info.get("code") != 0:
            raise RuntimeError(
                f"读取视频信息失败（{bvid}）：{info.get('message') or info.get('code')}"
            )

        data = info["data"]
        tracks = _chinese_tracks(
            await _subtitle_tracks(client, data["aid"], data["cid"], bvid, headers)
        )
        if not tracks:
            raise RuntimeError(f"这条视频没有中文字幕（含 AI 字幕）：{bvid}")

        for track in tracks:
            url = str(track.get("subtitle_url", ""))
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            body = (await _get_json(client, url, headers)).get("body", [])
            lines = [str(item.get("content", "")).strip() for item in body]
            text = "\n".join(line for line in lines if line)
            if text:
                log_event(
                    "bilibili_subtitle",
                    bvid=bvid,
                    lang=str(track.get("lan_doc", "?")),
                    lines=len(lines),
                )
                return text

    raise RuntimeError(f"字幕轨存在但内容为空：{bvid}")


async def list_user_videos(uid: int) -> list[dict[str, Any]]:
    """列出一位 UP 主的视频，按发布时间从新到旧。

    翻页在触发风控（412）时停止而不是重试——**风控一旦触发，
    整个账号的接口会短时间不可用**，硬重试只会让情况更糟。
    已经拿到的那几页照常返回，调用方按需要处理。

    这里不设固定页数上限。知识库订阅要的是「尽量完整发现」，
    所以一直翻到空页、重复页或风控为止；真正控制压力的是调用方的
    列表缓存、请求间隔、字幕导入预算和冷静期。
    """
    credential = _credential()
    space = user.User(uid, credential=credential)

    videos: list[dict[str, Any]] = []
    seen_bvids: set[str] = set()
    page = 1
    while True:
        try:
            payload = await space.get_videos(ps=30, pn=page)
        except Exception as exc:
            log_event(
                "bilibili_list_stop",
                uid=uid,
                page=page,
                reason="risk_control" if "412" in str(exc) else str(exc)[:80],
            )
            break

        page_videos = payload.get("list", {}).get("vlist", [])
        if not page_videos:
            break

        new_page_videos = [
            video for video in page_videos
            if video.get("bvid") and video["bvid"] not in seen_bvids
        ]
        if not new_page_videos:
            log_event("bilibili_list_stop", uid=uid, page=page, reason="duplicate_page")
            break

        for video in new_page_videos:
            seen_bvids.add(video["bvid"])
        videos.extend(new_page_videos)
        page += 1
        await asyncio.sleep(PAGE_PAUSE_SECONDS)

    log_event("bilibili_list", uid=uid, count=len(videos))
    return videos
