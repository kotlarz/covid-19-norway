import os
import json
import time
import pickle
import urllib3
import traceback
from covid import get_current_data, LOCALE_MAPPING
from datetime import datetime
from pprint import pprint

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", None)

if SLACK_WEBHOOK_URL is None:
    print("ERROR: Set SLACK_WEBHOOK_URL env variable")
    exit(1)


INITIAL_SLACK_MESSAGE = {
    "username": "COVID-19",
    "icon_emoji": ":biohazard_sign:",
    "channel": "#covid-19",
    "blocks": [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":biohazard_sign: *COVID-19 OPPDATERING* :biohazard_sign:",
                }
            ],
        },
    ],
}

STATE_FILE = "state.pkl"

# Check every 5 minutes
SLEEP_DURATION = 60


def get_state():
    with open(STATE_FILE, "rb") as handle:
        state = pickle.load(handle)
        return state


def set_state(data):
    with open(STATE_FILE, "wb") as handle:
        pickle.dump(
            {"last_updated": datetime.utcnow(), "data": data},
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )


def get_state_changes(result):
    state = get_state()
    data = state["data"]
    if not data:
        # Initial data, no changes!
        return None

    if (
        data["totals"]["confirmed"] == result["totals"]["confirmed"]
        and data["totals"]["dead"] == result["totals"]["dead"]
        and data["totals"]["recovered"] == result["totals"]["recovered"]
    ):
        # No changes, yay!
        return None

    changes = {
        "last_updated": state["last_updated"],
        "totals": result["totals"],
        "cases": [],
    }
    total_changes = {
        "confirmed": result["totals"]["confirmed"] - data["totals"]["confirmed"],
        "dead": result["totals"]["dead"] - data["totals"]["dead"],
        "recovered": result["totals"]["recovered"] - data["totals"]["recovered"],
    }

    changes["totals"]["changes"] = total_changes

    for municipality in result["cases"]:
        if municipality["name"] == "Ukjent":
            stored_municipality = next(
                (m for m in data["cases"] if m["name"] == "Ukjent"), None,
            )
        else:
            stored_municipality = next(
                (
                    m
                    for m in data["cases"]
                    if m["name"] != "Ukjent"
                    and m["municipalityCode"] == municipality["municipalityCode"]
                ),
                None,
            )

        municipality_changes = {"is_new": False}

        if stored_municipality is None:
            # New municipality!
            municipality_changes["is_new"] = True
            confirmed_diff = municipality["confirmed"]
            dead_diff = municipality["dead"]
            recovered_diff = municipality["recovered"]
        else:
            confirmed_per_1k_capita_diff = (
                municipality["confirmedPer1kCapita"]
                - stored_municipality["confirmedPer1kCapita"]
            )
            confirmed_diff = (
                municipality["confirmed"] - stored_municipality["confirmed"]
            )
            dead_diff = municipality["dead"] - stored_municipality["dead"]
            recovered_diff = (
                municipality["recovered"] - stored_municipality["recovered"]
            )

        if (
            confirmed_per_1k_capita_diff == 0
            and confirmed_diff == 0
            and dead_diff == 0
            and recovered_diff == 0
        ):
            continue

        municipality_changes["confirmedPer1kCapita"] = confirmed_per_1k_capita_diff
        municipality_changes["confirmed"] = confirmed_diff
        municipality_changes["dead"] = dead_diff
        municipality_changes["recovered"] = recovered_diff

        municipality["changes"] = municipality_changes
        changes["cases"].append(municipality)

    return changes


def generate_text_block(text):
    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": text},},
    ]


def format_number_text(data):
    text = ""
    for key, value in data["changes"].items():
        if key in ["confirmedPer1kCapita", "is_new"]:
            # TODO: use this for something?
            continue
        if value > 0:
            arrow = "▲ "
        elif value < 0:
            arrow = "▼ "
        else:
            arrow = ""

        value = str("+" + str(value) if value > 0 else value)
        text += (
            "\n_"
            + LOCALE_MAPPING[key]
            + "_: *"
            + str(data[key])
            + "* | Endring: *"
            + arrow
            + str(value)
            + "*"
        )

    return text


def format_slack_message(changes):
    slack_message = INITIAL_SLACK_MESSAGE.copy()

    text = "Tidligere status oppdatering: " + str(changes["last_updated"].isoformat())
    slack_message["blocks"].append(
        {"type": "context", "elements": [{"text": text, "type": "mrkdwn",}],},
    )

    text = "*:flag-no: Landsbasis :flag-no:*"
    text += format_number_text(changes["totals"])
    slack_message["blocks"].extend(generate_text_block(text))

    for municipality in changes["cases"]:
        text = "*" + municipality["name"]
        if "parent" in municipality:
            text += " (" + municipality["parent"] + ")"
        text += "*"

        if municipality["changes"]["is_new"]:
            text += " :new:"

        text += format_number_text(municipality)
        slack_message["blocks"].extend(generate_text_block(text))
    return slack_message


def send_slack_message(slack_message):
    if len(slack_message["blocks"]) > 50:
        print(
            "Limit of 50 slack message blocks... splitting up the blocks and sending them seperate"
        )
        blocks = slack_message["blocks"]
        blocks_list = [blocks[x : x + 50] for x in range(0, len(blocks), 50)]
        for inner_blocks in blocks_list:
            slack_message["blocks"] = inner_blocks
            send_slack_message(slack_message)
        return

    http = urllib3.PoolManager()
    r = http.request(
        "POST",
        SLACK_WEBHOOK_URL,
        body=json.dumps(slack_message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    if r.status == 200:
        print("Sent message to Slack")
        return

    print("Error whilst sending message to Slack:")
    print(r.data)


if not os.path.isfile(STATE_FILE):
    # Set an empty state
    set_state({})


while True:
    try:
        print("=" * 32)
        print("Fetching data...")
        data = get_current_data()
        changes = get_state_changes(data)

        print("-" * 32)
        print("Current:")
        for key, value in data["totals"].items():
            print(LOCALE_MAPPING.get(key, key), "=", value)

        print("-" * 32)
        if changes is None:
            print("No changes since last check!")
            time.sleep(SLEEP_DURATION)
            continue

        print("-" * 32)
        print("Differences:")
        for key, value in changes["totals"]["changes"].items():
            print(LOCALE_MAPPING.get(key, key), "=", value)

        print("Oh no, found changes in the data...")
        slack_message = format_slack_message(changes)
        send_slack_message(slack_message)
        set_state(data)
    except Exception as e:
        print("Error whilst processing")
        traceback.print_exc()
        pass

    print("Sleeping for", SLEEP_DURATION, "seconds")
    time.sleep(SLEEP_DURATION)
