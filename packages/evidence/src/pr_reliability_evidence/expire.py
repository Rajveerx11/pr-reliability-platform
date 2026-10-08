"""Run one expiry sweep; schedule at least once per minute in production."""

import os

import psycopg

from .store import expire


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        while True:
            with connection.transaction():
                count = expire(connection)
            if count < 100:
                break


if __name__ == "__main__":
    main()
