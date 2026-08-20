"""LLM 응답 캐시/재생 계층 — 테스트가 API 쿼터를 태우지 않게 한다.

환경변수 LLM_MODE 로 동작을 바꾼다.

    live   (기본) 항상 실제 호출. 캐시를 읽지도 쓰지도 않는다. 운영용.
    cache        캐시에 있으면 재사용, 없으면 호출 후 저장. 로컬 반복 테스트용.
    replay       캐시에만 의존. 미스면 예외. 네트워크·쿼터를 전혀 쓰지 않아 CI 용.
    off          호출을 무조건 차단. LLM 장애 시 폴백 경로를 검증할 때 쓴다.

같은 (모델, 프롬프트) 조합은 같은 키를 갖는다. temperature 가 0 이라 응답이
사실상 결정적이므로, 하루에 몇 번을 돌리든 프롬프트가 그대로면 호출은 1회다.
"""

import hashlib
import json
import os
from pathlib import Path

DEFAULT_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "llm_cache"

_VALID_MODES = ("live", "cache", "replay", "off")


class CacheMiss(RuntimeError):
    """replay 모드에서 캐시에 없는 프롬프트를 요청했을 때."""


class CallBlocked(RuntimeError):
    """off 모드에서 호출을 시도했을 때."""


def mode() -> str:
    value = os.environ.get("LLM_MODE", "live").strip().lower()
    return value if value in _VALID_MODES else "live"


def cache_dir() -> Path:
    override = os.environ.get("LLM_CACHE_DIR", "").strip()
    return Path(override) if override else DEFAULT_CACHE_DIR


def _key(model: str, instruction: str) -> str:
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(instruction.encode("utf-8"))
    return digest.hexdigest()[:32]


def _entry_path(model: str, instruction: str) -> Path:
    return cache_dir() / f"{_key(model, instruction)}.json"


def lookup(model: str, instruction: str):
    """캐시된 응답 문자열, 없으면 None. live 모드는 항상 None."""
    if mode() == "live":
        return None
    path = _entry_path(model, instruction)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)["response"]
    except (OSError, ValueError, KeyError):
        return None


def store(model: str, instruction: str, response: str) -> None:
    """cache 모드에서만 저장한다. 저장 실패가 파이프라인을 막지 않는다."""
    if mode() != "cache":
        return
    path = _entry_path(model, instruction)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model": model, "instruction": instruction, "response": response}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠️ LLM 캐시 저장 실패({e}) — 진행에는 영향 없음")


def guard_network(model: str) -> None:
    """캐시 미스 이후 실제 호출로 넘어가도 되는지 확인한다."""
    current = mode()
    if current == "off":
        raise CallBlocked(f"LLM_MODE=off — {model} 호출 차단됨")
    if current == "replay":
        raise CacheMiss(
            f"LLM_MODE=replay 인데 {model} 프롬프트가 캐시에 없습니다. "
            f"LLM_MODE=cache 로 한 번 녹화한 뒤 다시 실행하세요."
        )
