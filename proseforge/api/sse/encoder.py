import orjson


def encode_sse(*, event_id: str, event: str, data: dict[str, object]) -> bytes:
    payload = orjson.dumps(data).decode()
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n".encode()
