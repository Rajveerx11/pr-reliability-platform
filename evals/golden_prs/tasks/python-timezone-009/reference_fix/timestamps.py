from datetime import datetime


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
