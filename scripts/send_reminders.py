"""Deliver due reminders once: `python -m scripts.send_reminders`."""
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    from palmer.reminders import send_due_reminders
    send_due_reminders()


if __name__ == "__main__":
    main()
