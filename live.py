import os
import json
import time
import pickle
import urllib3
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
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":biohazard_sign: *COVID-19 OPPDATERING* :biohazard_sign:",
            },
        }
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
    total_changes = {}

    confirmed_diff = result["totals"]["confirmed"] - data["totals"]["confirmed"]
    dead_diff = result["totals"]["dead"] - data["totals"]["dead"]
    recovered_diff = result["totals"]["recovered"] - data["totals"]["recovered"]

    if confirmed_diff != 0:
        total_changes["confirmed"] = confirmed_diff
    if dead_diff != 0:
        total_changes["dead"] = dead_diff
    if recovered_diff != 0:
        total_changes["recovered"] = recovered_diff

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
                    if m["municipalityCode"] == municipality["municipalityCode"]
                ),
                None,
            )

        confirmed_per_1k_capita_diff = (
            municipality["confirmedPer1kCapita"]
            - stored_municipality["confirmedPer1kCapita"]
        )
        confirmed_diff = municipality["confirmed"] - stored_municipality["confirmed"]
        dead_diff = municipality["dead"] - stored_municipality["dead"]
        recovered_diff = municipality["recovered"] - stored_municipality["recovered"]

        municipality_changes = {}

        if (
            confirmed_per_1k_capita_diff == 0
            and confirmed_diff == 0
            and dead_diff == 0
            and recovered_diff == 0
        ):
            continue

        if confirmed_per_1k_capita_diff != 0:
            municipality_changes["confirmedPer1kCapita"] = confirmed_per_1k_capita_diff
        if confirmed_diff != 0:
            municipality_changes["confirmed"] = confirmed_diff
        if dead_diff != 0:
            municipality_changes["dead"] = dead_diff
        if recovered_diff != 0:
            municipality_changes["recovered"] = recovered_diff

        municipality["changes"] = municipality_changes
        changes["cases"].append(municipality)

    return changes


def format_number_text(data):
    text = ""
    for key, value in data["changes"].items():
        if key == "confirmedPer1kCapita":
            # TODO: use this for something?
            continue
        arrow = "▲" if value > 0 else "▼"
        value = str("+" + str(value) if value > 0 else value)
        text += (
            "\n_"
            + LOCALE_MAPPING[key]
            + "_: "
            + arrow
            + " *"
            + str(value)
            + "* | Nåværende: *"
            + str(data[key])
            + "*"
        )

    return text


def format_slack_message(changes):
    slack_message = INITIAL_SLACK_MESSAGE.copy()

    text = (
        "Tid siden siste Slack melding: "
        + str(datetime.utcnow() - changes["last_updated"]).split(".", 2)[0]
    )
    slack_message["blocks"].append(
        {"type": "context", "elements": [{"text": text, "type": "mrkdwn",}],},
    )

    text = "*:flag-no: Landsbasis :flag-no:*"
    text += format_number_text(changes["totals"])
    slack_message["blocks"].extend(
        [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": text},},
        ]
    )

    for municipality in changes["cases"]:
        text = f"*{municipality['name']} ({municipality['parent']})*"
        text += format_number_text(municipality)
        slack_message["blocks"].extend(
            [
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ]
        )
    return slack_message


def send_slack_message(slack_message):
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

result = get_current_data()

while True:
    try:
        changes = get_state_changes(result)

        if changes is None:
            print("No changes since last check!")
            set_state(result)
            time.sleep(SLEEP_DURATION)
            continue

        print("Oh no, found changes in the data...")
        slack_message = format_slack_message(changes)
        send_slack_message(slack_message)
        set_state(result)
    except:
        pass

    time.sleep(SLEEP_DURATION)