from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests
import yaml
from ruamel.yaml import YAML

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
FREQUENCY_FILE = CONFIG_DIR / "frequency_words.txt"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
TIMELINE_FILE = CONFIG_DIR / "timeline.yaml"
SCHEDULE_FILE = CONFIG_DIR / "local_schedule.json"
UI_DIR = ROOT_DIR / "config_ui"

# --- 修改后的 Prompt ---
AI_SUGGEST_PROMPT = """你是一个专业的关键词扩展引擎。
用户会输入一个或多个公司名或主题。请为每个输入项生成一行对应的正则过滤规则。

【输出格式要求】
每一行必须严格遵守：/正则内容/ => 标注名

【生成逻辑】
1. 识别多个主体：如果用户输入“华为, 苹果”，请为每个主体生成一行。
2. 关键词扩展：包含核心产品、系统、高频人物等。
   - 华为：包含 华为、Huawei、Mate、鸿蒙、HarmonyOS、任正非 等。
   - 苹果：包含 苹果、Apple、iPhone、iPad、iOS、库克 等。
3. 正则规范：
   - 关键词之间用 | 分隔。
   - 英文单词（如 Apple）前后必须加 \b。
   - 不要解释，不要代码块，不要标题。

【正确输出示例】
/苹果|\\bApple\\b|iPhone|iPad|\\biOS\\b|库克/ => 苹果
/华为|\\bHuawei\\b|Mate|鸿蒙|任正非|\\bHarmonyOS\\b/ => 华为
"""

_yaml_roundtrip = YAML()
_yaml_roundtrip.preserve_quotes = True


def _load_config() -> Dict[str, Any]:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = _yaml_roundtrip.load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml 格式异常")
    return data


def _save_config(data: Dict[str, Any]) -> None:
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        _yaml_roundtrip.dump(data, f)


def _load_ai_config() -> Dict[str, Any]:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data.get("ai", {}) or {}


def _extract_header_lines(lines: List[str]) -> List[str]:␊
    header = []
    for line in lines:
        if line.strip().startswith("["):
            break
        if line.strip() == "" or line.lstrip().startswith("#"):
            header.append(line.rstrip("\n"))
            continue
        break
    return header


def _load_timeline_presets() -> List[str]:
    if not TIMELINE_FILE.exists():
        return ["always_on", "morning_evening", "office_hours", "night_owl", "custom"]

    with TIMELINE_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return ["always_on", "morning_evening", "office_hours", "night_owl", "custom"]

    presets = data.get("presets") or {}
    preset_names = list(presets.keys()) if isinstance(presets, dict) else []
    if "custom" not in preset_names:
        preset_names.append("custom")
    return preset_names


def _read_frequency_sections() -> Dict[str, str]:
    if not FREQUENCY_FILE.exists():
        return {"global_filter": "", "regex": ""}

    content = FREQUENCY_FILE.read_text(encoding="utf-8")
    lines = content.splitlines()

    current_section = None
    global_filter_lines: List[str] = []
    regex_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "[GLOBAL_FILTER]":
            current_section = "GLOBAL_FILTER"
            continue
        if stripped == "[WORD_GROUPS]":
            current_section = "WORD_GROUPS"
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = None
            continue

        if current_section == "GLOBAL_FILTER":
            global_filter_lines.append(line)
        elif current_section == "WORD_GROUPS":
            regex_lines.append(line)

    return {
        "global_filter": "\n".join(global_filter_lines).strip(),
        "regex": "\n".join(regex_lines).strip(),
    }


def _load_custom_plan() -> Dict[str, Any]:
    if not SCHEDULE_FILE.exists():
        return {
            "start": "09:00",
            "end": "18:00",
            "frequency_minutes": 60,
        }

    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid local schedule")
        return {
            "start": str(data.get("start") or "09:00"),
            "end": str(data.get("end") or "18:00"),
            "frequency_minutes": int(data.get("frequency_minutes") or 60),
        }
    except Exception:
        return {
            "start": "09:00",
            "end": "18:00",
            "frequency_minutes": 60,
        }


def _valid_hhmm(value: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value or ""))

# --- 修改：同时支持 GLOBAL_FILTER 和 WORD_GROUPS 写入 ---
def _build_frequency_content(regex: str, global_filter: str) -> str:
    existing_lines = FREQUENCY_FILE.read_text(encoding="utf-8").splitlines() if FREQUENCY_FILE.exists() else []
    header = _extract_header_lines(existing_lines)
    content_lines = []
    if header:
        content_lines.extend(header)
    content_lines.extend(
        [
            "",
            "[GLOBAL_FILTER]",
            global_filter.strip(),
            "",
            "[WORD_GROUPS]",
            regex.strip(),
            "",
        ]
    )
    return "\n".join(content_lines) + "\n"


class ConfigRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def do_GET(self) -> None:␊
        parsed = urlparse(self.path)␊
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_file(UI_DIR / "index.html")
            return
        if parsed.path == "/api/config":
            self._handle_get_config()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "未找到资源")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/suggest":
            self._handle_suggest()
            return
        if parsed.path != "/api/config":
            self.send_error(HTTPStatus.NOT_FOUND, "未找到接口")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))

        response: Dict[str, Any] = {"updated": []}

        # --- 核心修改：接收两个格子的内容 ---
        regex = (payload.get("regex") or "").strip()
        global_filter = (payload.get("global_filter") or "").strip()
        
        FREQUENCY_FILE.write_text(_build_frequency_content(regex, global_filter), encoding="utf-8")
        response["updated"].append("frequency_words.txt")

        schedule = payload.get("schedule") or {}
        if schedule:
            data = _load_config()
            schedule_config = data.setdefault("schedule", {})

            presets = _load_timeline_presets()
            preset = str(schedule.get("preset") or "morning_evening")
            if preset not in presets:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"detail": f"无效 schedule.preset: {preset}，可选: {', '.join(presets)}"},
                )
                return

            schedule_config["enabled"] = bool(schedule.get("enabled", True))
            schedule_config["preset"] = preset
            _save_config(data)
            response["updated"].append("config.yaml:schedule")

            custom_plan = payload.get("custom_plan") or {}
            if preset == "custom" and custom_plan:
                start = str(custom_plan.get("start") or "09:00")
                end = str(custom_plan.get("end") or "18:00")
                frequency_minutes = int(custom_plan.get("frequency_minutes") or 60)

                if not (_valid_hhmm(start) and _valid_hhmm(end)):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"detail": "自定义时段格式错误，请使用 HH:MM"},
                    )
                    return

                if frequency_minutes <= 0:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"detail": "发送频率必须为正整数（分钟）"},
                    )
                    return

                SCHEDULE_FILE.write_text(
                    json.dumps(
                        {
                            "start": start,
                            "end": end,
                            "frequency_minutes": frequency_minutes,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                response["updated"].append("local_schedule.json")

        report_mode = (payload.get("report_mode") or "").strip()
        if report_mode:
            if report_mode not in {"daily", "current", "incremental"}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"detail": "report_mode 仅支持 daily/current/incremental"},
                )
                return

            data = _load_config()
            report = data.setdefault("report", {})
            report["mode"] = report_mode
            _save_config(data)
            response["updated"].append("config.yaml:report.mode")

        if "ai_analysis_enabled" in payload:
            data = _load_config()
            ai_analysis = data.setdefault("ai_analysis", {})
            ai_analysis["enabled"] = bool(payload.get("ai_analysis_enabled"))
            _save_config(data)
            response["updated"].append("config.yaml:ai_analysis.enabled")

        self._send_json(HTTPStatus.OK, response)

    def _handle_get_config(self) -> None:
        data = _load_config()
        schedule = data.get("schedule") or {}
        report = data.get("report") or {}
        ai_analysis = data.get("ai_analysis") or {}
        freq = _read_frequency_sections()

        self._send_json(
            HTTPStatus.OK,
            {
                "regex": freq.get("regex", ""),
                "global_filter": freq.get("global_filter", ""),
                "schedule": {
                    "enabled": bool(schedule.get("enabled", True)),
                    "preset": schedule.get("preset", "morning_evening"),
                    "presets": _load_timeline_presets(),
                },
                "custom_plan": _load_custom_plan(),
                "report_mode": report.get("mode", "current"),
                "ai_analysis_enabled": bool(ai_analysis.get("enabled", True)),
            },
        )

    def _handle_suggest(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))
        keyword = (payload.get("keyword") or "").strip()

        ai_config = _load_ai_config()
        api_key = ai_config.get("api_key", "")
        model = ai_config.get("model", "")
        api_base = ai_config.get("api_base", "") or "https://api.openai.com/v1"
        
        clean_model = model.replace("openai/", "")
        url = api_base.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        body = {
            "model": clean_model,
            "messages": [
                {"role": "system", "content": AI_SUGGEST_PROMPT},
                {"role": "user", "content": f"主题词：{keyword}"},
            ],
            "temperature": 0.3
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            data = resp.json()
            # 直接提取 AI 的完整回复内容，不再做单行正则提取
            content = data["choices"][0]["message"]["content"].strip()
            self._send_json(HTTPStatus.OK, {"regex": content})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"detail": str(exc)})


def main() -> None:
    server = HTTPServer(("0.0.0.0", 8090), ConfigRequestHandler)
    print("TrendRadar 配置中心已启动: http://localhost:8090")
    server.serve_forever()


if __name__ == "__main__":
    main()
