"""GitHub Actions 에서 도는 발행기 — 예약 시각이 된 게시물을 인스타에 올린다.

PC 가 꺼져 있어도 이 파일이 GitHub 서버에서 15분마다 실행되어 발행한다.
이 저장소의 `queue/<id>.json` 에 예약이 들어 있고, 사진은 `queue/<id>/01.jpg…` 에 있다.
사진 주소는 지금 커밋 번호(SHA)를 붙여 만든다 — 캐시 때문에 옛 사진이 넘어가는 일을 막는다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://graph.instagram.com"
ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
DONE = ROOT / "published"
TOKEN = os.environ["IG_ACCESS_TOKEN"]
USER_ID = os.environ["IG_USER_ID"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
SHA = os.environ.get("GITHUB_SHA", "main")


def call(method: str, path: str, **params) -> dict:
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    req = urllib.request.Request(url + ("?" + data.decode() if method == "GET" else ""),
                                 data=None if method == "GET" else data, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    if "error" in body:
        raise RuntimeError(body["error"].get("message", str(body["error"])))
    return body


def wait_ready(cid: str, timeout_s: int = 300) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        st = call("GET", cid, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 상태 {st}")
        time.sleep(5)
    raise RuntimeError("컨테이너 처리 시간 초과")


def publish(entry: dict) -> dict:
    urls = [f"https://raw.githubusercontent.com/{REPO}/{SHA}/{p}" for p in entry["images"]]
    if len(urls) == 1:
        cid = call("POST", f"{USER_ID}/media", image_url=urls[0], caption=entry["caption"])["id"]
        wait_ready(cid)
        parent = cid
    else:
        children = []
        for u in urls:
            c = call("POST", f"{USER_ID}/media", image_url=u, is_carousel_item="true")["id"]
            children.append(c)
        for c in children:
            wait_ready(c)
        parent = call("POST", f"{USER_ID}/media", media_type="CAROUSEL",
                      children=",".join(children), caption=entry["caption"])["id"]
        wait_ready(parent)
    media = call("POST", f"{USER_ID}/media_publish", creation_id=parent)
    info = call("GET", media["id"], fields="id,permalink,timestamp")
    return info


def main() -> None:
    now = datetime.now(timezone.utc)
    if not QUEUE.is_dir():
        print("예약된 게시물이 없습니다.")
        return
    due = []
    for f in sorted(QUEUE.glob("*.json")):
        entry = json.loads(f.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(entry["publish_at"].replace("Z", "+00:00"))
        if at <= now:
            due.append((f, entry))
        else:
            print(f"{entry['id']}: 예약 {at.isoformat()} — 아직 아님")
    if not due:
        return
    DONE.mkdir(exist_ok=True)
    for f, entry in due[:1]:  # 한 번에 한 건만 (하루 1건 원칙)
        print(f"{entry['id']} 발행 시작 — 사진 {len(entry['images'])}장")
        try:
            info = entry_result = publish(entry)
            status = "published"
        except Exception as exc:  # noqa: BLE001
            entry["tries"] = entry.get("tries", 0) + 1
            print(f"실패({entry['tries']}회): {exc}")
            if entry["tries"] < 3:
                f.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
                return
            info, status = {"error": str(exc)}, "failed"
            entry_result = info
        (DONE / f.name).write_text(json.dumps(
            {**entry, "status": status, "result": entry_result,
             "finished": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        f.unlink()
        folder = QUEUE / entry["id"]
        if folder.is_dir():
            for p in folder.iterdir():
                p.unlink()
            folder.rmdir()
        print(f"{status}: {entry_result.get('permalink', '')}")


if __name__ == "__main__":
    main()
