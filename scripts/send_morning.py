"""Trigger the morning job once: `python -m scripts.send_morning`.

Respects each user's send window and the per-day guard, so running it by hand
cannot double-send."""
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    from palmer.morning import send_morning_messages
    send_morning_messages()


if __name__ == "__main__":
    main()
