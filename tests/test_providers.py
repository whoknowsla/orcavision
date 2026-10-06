import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PNG = b"\x89PNG fake image"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": json.loads(json.dumps(payload)),
                           "headers": headers, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def p(submodules):
    return submodules.providers


def ok(p, body):
    return p.HttpResponse(200, body, json.dumps(body))


def err(p, status, body):
    text = json.dumps(body) if not isinstance(body, str) else body
    return p.HttpResponse(status, body if not isinstance(body, str) else None, text)


def request(p, **overrides):
    values = dict(model="m", prompt="Describe", image_png=PNG, max_output_tokens=300,
                  temperature=0.5, timeout=30, api_key="key", host="http://127.0.0.1:11434")
    values.update(overrides)
    return p.DescribeRequest(**values)


OPENAI_OK = {
    "status": "completed",
    "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": "  A login form.  "}]},
    ],
}


def test_openai_sends_image_and_parses_output_text(p):
    transport = FakeTransport(ok(p, OPENAI_OK))
    text = p.get_provider("openai").describe(request(p, model="gpt-4o"), transport)

    assert text == "A login form."
    call = transport.calls[0]
    assert call["url"] == "https://api.openai.com/v1/responses"
    assert call["headers"] == {"Authorization": "Bearer key"}
    assert call["timeout"] == 30
    payload = call["payload"]
    assert payload["model"] == "gpt-4o"
    assert payload["max_output_tokens"] == 300
    assert payload["temperature"] == 0.5
    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "Describe"}
    assert content[1]["image_url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_openai_retries_without_temperature_when_model_rejects_it(p):
    rejected = err(p, 400, {"error": {"message": "Unsupported parameter: 'temperature'."}})
    transport = FakeTransport(rejected, ok(p, OPENAI_OK))

    assert p.get_provider("openai").describe(request(p), transport) == "A login form."
    assert "temperature" in transport.calls[0]["payload"]
    assert "temperature" not in transport.calls[1]["payload"]


@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": {"message": "Incorrect API key"}}, "OpenAI rejected the API key."),
    (429, {"error": {"message": "quota"}}, "OpenAI rate limit or quota exceeded. quota"),
    (500, "<html>oops</html>", "OpenAI error 500: <html>oops</html>"),
])
def test_openai_http_errors(p, status, body, expected):
    transport = FakeTransport(err(p, status, body))
    with pytest.raises(p.DescribeError) as info:
        p.get_provider("openai").describe(request(p), transport)
    assert str(info.value) == expected


def test_openai_incomplete_response_explains_token_limit(p):
    body = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning"}]}
    with pytest.raises(p.DescribeError, match="Increase the maximum output tokens"):
        p.get_provider("openai").describe(request(p), FakeTransport(ok(p, body)))


def test_openai_refusal_is_reported(p):
    body = {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No."}]}]}
    with pytest.raises(p.DescribeError, match="OpenAI refused: No."):
        p.get_provider("openai").describe(request(p), FakeTransport(ok(p, body)))


def test_openai_requires_key_before_sending(p):
    transport = FakeTransport()
    with pytest.raises(p.DescribeError, match="needs an API key.*OPENAI_API_KEY"):
        p.get_provider("openai").describe(request(p, api_key=""), transport)
    assert transport.calls == []


def test_timeout_and_connection_errors_become_describe_errors(p):
    provider = p.get_provider("openai")
    with pytest.raises(p.DescribeError, match="OpenAI did not answer in time."):
        provider.describe(request(p), FakeTransport(TimeoutError()))
    with pytest.raises(p.DescribeError, match="Could not connect to OpenAI"):
        provider.describe(request(p), FakeTransport(ConnectionError("refused")))


def test_gemini_sends_inline_image_and_skips_thoughts(p):
    body = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "thinking...", "thought": True},
        {"text": "A terminal "},
        {"text": "window."},
    ]}}]}
    transport = FakeTransport(ok(p, body))
    text = p.get_provider("gemini").describe(request(p, model="models/gemini-2.5-flash"), transport)

    assert text == "A terminal window."
    call = transport.calls[0]
    assert call["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                           "gemini-2.5-flash:generateContent")
    assert call["headers"] == {"x-goog-api-key": "key"}
    parts = call["payload"]["contents"][0]["parts"]
    assert parts[0] == {"text": "Describe"}
    assert parts[1]["inline_data"] == {"mime_type": "image/png",
                                       "data": base64.b64encode(PNG).decode()}
    assert call["payload"]["generationConfig"] == {"maxOutputTokens": 300, "temperature": 0.5}


@pytest.mark.parametrize("body,expected", [
    ({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]},
     "Increase the maximum output tokens"),
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "Gemini blocked the request: SAFETY."),
    ({"candidates": []}, "Gemini returned no description."),
])
def test_gemini_empty_responses(p, body, expected):
    with pytest.raises(p.DescribeError, match=expected):
        p.get_provider("gemini").describe(request(p), FakeTransport(ok(p, body)))


@pytest.mark.parametrize("status,body,expected", [
    (400, {"error": {"message": "API key not valid", "details": [{"reason": "API_KEY_INVALID"}]}},
     "Gemini rejected the API key."),
    (404, {"error": {"message": "models/x is not found"}}, "Gemini model 'm' was not found."),
])
def test_gemini_http_errors(p, status, body, expected):
    with pytest.raises(p.DescribeError) as info:
        p.get_provider("gemini").describe(request(p), FakeTransport(err(p, status, body)))
    assert str(info.value) == expected


def test_ollama_sends_generate_request(p):
    transport = FakeTransport(ok(p, {"response": " A cat. ", "done": True}))
    text = p.get_provider("ollama").describe(
        request(p, model="llava", host="http://localhost:11434/", api_key=""), transport)

    assert text == "A cat."
    call = transport.calls[0]
    assert call["url"] == "http://localhost:11434/api/generate"
    assert call["payload"] == {
        "model": "llava",
        "prompt": "Describe",
        "images": [base64.b64encode(PNG).decode()],
        "stream": False,
        "options": {"num_predict": 300, "temperature": 0.5},
    }


def test_ollama_missing_model_suggests_pull(p):
    response = err(p, 404, {"error": "model 'llava' not found"})
    with pytest.raises(p.DescribeError, match="Run: ollama pull llava"):
        p.get_provider("ollama").describe(request(p, model="llava"), FakeTransport(response))


def test_ollama_connection_error_names_host(p):
    with pytest.raises(p.DescribeError, match=r"Ollama at http://127.0.0.1:11434\. Is ollama serve"):
        p.get_provider("ollama").describe(request(p), FakeTransport(ConnectionError("refused")))


@pytest.mark.parametrize("overrides,expected", [
    ({"host": "localhost:11434"}, "must start with http"),
    ({"model": ""}, "Choose an Ollama model"),
])
def test_ollama_validates_settings(p, overrides, expected):
    with pytest.raises(p.DescribeError, match=expected):
        p.get_provider("ollama").describe(request(p, **overrides), FakeTransport())


def test_unknown_provider(p):
    with pytest.raises(p.DescribeError, match="Unknown provider 'nope'."):
        p.get_provider("nope")


def test_post_json_round_trip_and_http_errors(p):
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received["auth"] = self.headers["Authorization"]
            status = 200 if self.path == "/ok" else 503
            payload = json.dumps({"path": self.path}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        response = p.post_json(f"{base}/ok", {"a": 1}, {"Authorization": "Bearer x"}, 5)
        assert (response.status, response.body) == (200, {"path": "/ok"})
        assert received == {"body": {"a": 1}, "auth": "Bearer x"}

        response = p.post_json(f"{base}/fail", {}, {}, 5)
        assert (response.status, response.body) == (503, {"path": "/fail"})
    finally:
        server.shutdown()
        server.server_close()

    with pytest.raises(ConnectionError):
        p.post_json(f"{base}/ok", {}, {}, 5)


ANTHROPIC_OK = {
    "type": "message",
    "stop_reason": "end_turn",
    "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": " A settings dialog. "}],
}


def test_anthropic_request_for_current_model(p):
    transport = FakeTransport(ok(p, ANTHROPIC_OK))
    text = p.get_provider("anthropic").describe(request(p, model="claude-opus-5-5"), transport)

    assert text == "A settings dialog."
    call = transport.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"] == {"x-api-key": "key", "anthropic-version": "2023-06-01",
                               "anthropic-beta": "server-side-fallback-2026-07-01"}
    payload = call["payload"]
    assert payload["model"] == "claude-opus-5-5"
    assert payload["fallbacks"] == "default"
    assert payload["max_tokens"] == 16000
    assert "temperature" not in payload
    image, text_block = payload["messages"][0]["content"]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(PNG).decode()}}
    assert text_block == {"type": "text", "text": "Describe"}


def test_anthropic_request_for_older_model(p):
    transport = FakeTransport(ok(p, ANTHROPIC_OK))
    p.get_provider("anthropic").describe(
        request(p, model="claude-haiku-4-5", temperature=1.5, max_output_tokens=20000), transport)

    call = transport.calls[0]
    assert "anthropic-beta" not in call["headers"]
    assert "fallbacks" not in call["payload"]
    assert call["payload"]["temperature"] == 1.0
    assert call["payload"]["max_tokens"] == 20000


def test_anthropic_retries_without_temperature(p):
    rejected = err(p, 400, {"type": "error", "error": {
        "type": "invalid_request_error", "message": "temperature is not supported"}})
    transport = FakeTransport(rejected, ok(p, ANTHROPIC_OK))
    p.get_provider("anthropic").describe(request(p, model="claude-future-1"), transport)
    assert "temperature" in transport.calls[0]["payload"]
    assert "temperature" not in transport.calls[1]["payload"]


@pytest.mark.parametrize("body,expected", [
    ({"stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber"},
      "content": [{"type": "text", "text": "Partial"}]},
     "Claude declined to describe this image (cyber)."),
    ({"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": ""}]},
     "Claude ran out of output tokens. Increase the maximum output tokens."),
    ({"stop_reason": "end_turn", "content": []}, "Claude returned no description."),
])
def test_anthropic_empty_or_refused(p, body, expected):
    with pytest.raises(p.DescribeError) as info:
        p.get_provider("anthropic").describe(request(p), FakeTransport(ok(p, body)))
    assert str(info.value) == expected


@pytest.mark.parametrize("status,expected", [
    (401, "Claude rejected the API key."),
    (404, "Claude model 'm' was not found."),
    (529, "Claude is overloaded. Try again shortly."),
    (500, "Claude error 500: boom"),
])
def test_anthropic_http_errors(p, status, expected):
    response = err(p, status, {"type": "error", "error": {"type": "x", "message": "boom"}})
    with pytest.raises(p.DescribeError) as info:
        p.get_provider("anthropic").describe(request(p), FakeTransport(response))
    assert str(info.value) == expected


def test_anthropic_requires_key(p):
    with pytest.raises(p.DescribeError, match="Claude needs an API key.*ANTHROPIC_API_KEY"):
        p.get_provider("anthropic").describe(request(p, api_key=""), FakeTransport())


COMPATIBLE_OK = {"choices": [{"finish_reason": "stop",
                              "message": {"role": "assistant", "content": " A chart. "}}]}


def test_compatible_request_without_key(p):
    transport = FakeTransport(ok(p, COMPATIBLE_OK))
    text = p.get_provider("openai-compatible").describe(
        request(p, model="qwen2.5-vl", api_key="", host="http://localhost:1234/v1/"), transport)

    assert text == "A chart."
    call = transport.calls[0]
    assert call["url"] == "http://localhost:1234/v1/chat/completions"
    assert call["headers"] == {}
    payload = call["payload"]
    assert payload["max_tokens"] == 300
    assert payload["temperature"] == 0.5
    text_part, image_part = payload["messages"][0]["content"]
    assert text_part == {"type": "text", "text": "Describe"}
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_compatible_adapts_rejected_parameters(p):
    transport = FakeTransport(
        err(p, 400, {"error": {"message": "Unsupported value: 'temperature'"}}),
        err(p, 400, {"error": {"message": "Use 'max_completion_tokens' instead."}}),
        ok(p, {"choices": [{"message": {"content": [{"type": "text", "text": "A map."}]}}]}),
    )
    text = p.get_provider("openai-compatible").describe(
        request(p, host="https://openrouter.ai/api/v1"), transport)

    assert text == "A map."
    assert transport.calls[0]["headers"] == {"Authorization": "Bearer key"}
    last = transport.calls[2]["payload"]
    assert "temperature" not in last
    assert last["max_completion_tokens"] == 300 and "max_tokens" not in last


@pytest.mark.parametrize("overrides,expected", [
    ({"host": ""}, "Set the base URL"),
    ({"host": "openrouter.ai"}, "Set the base URL"),
    ({"model": ""}, "Choose a model for the OpenAI-compatible server"),
])
def test_compatible_validates_settings(p, overrides, expected):
    with pytest.raises(p.DescribeError, match=expected):
        p.get_provider("openai-compatible").describe(request(p, **overrides), FakeTransport())


@pytest.mark.parametrize("response,expected", [
    ((401, {"error": {"message": "no"}}), "The server rejected the API key."),
    ((200, {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}),
     "The model ran out of output tokens"),
    ((200, {"choices": [{"message": {"content": None, "refusal": "Not allowed."}}]}),
     "The model refused: Not allowed."),
])
def test_compatible_errors(p, response, expected):
    status, body = response
    transport = FakeTransport(p.HttpResponse(status, body, json.dumps(body)))
    with pytest.raises(p.DescribeError, match=expected):
        p.get_provider("openai-compatible").describe(
            request(p, host="https://example.test/v1"), transport)


def test_compatible_connection_error_names_server(p):
    with pytest.raises(p.DescribeError, match=r"Could not connect to https://example.test/v1\."):
        p.get_provider("openai-compatible").describe(
            request(p, host="https://example.test/v1"), FakeTransport(ConnectionError("refused")))


def test_provider_registry(p):
    assert p.provider_names() == ["openai", "anthropic", "gemini", "ollama", "openai-compatible"]
    keyed = {name for name in p.provider_names() if p.get_provider(name).uses_api_key}
    assert keyed == {"openai", "anthropic", "gemini", "openai-compatible"}
