import os
import json
import datetime
import requests
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

def get_credentials():
    """
    Parses the service account credentials from the GOOGLE_CREDENTIALS_JSON env variable
    and returns a Credentials object with Sheet and Calendar scopes.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON environment variable is not set.")
    
    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse GOOGLE_CREDENTIALS_JSON as JSON: {e}")
        
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar"
    ]
    return service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)

def send_slack_dm(slack_id, message, token):
    """
    Sends a direct message to a user by their Slack ID.
    First opens a conversation using conversations.open, then posts the message.
    """
    if not slack_id:
        print("Skipping Slack DM: No Slack ID provided.")
        return False
    if not token:
        print("Skipping Slack DM: No SLACK_BOT_TOKEN provided.")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }

    # Open direct message channel with the user to get a channel ID
    open_url = "https://slack.com/api/conversations.open"
    open_payload = {"users": slack_id}
    
    channel_id = slack_id
    try:
        open_res = requests.post(open_url, json=open_payload, headers=headers, timeout=10)
        if open_res.status_code == 200:
            res_json = open_res.json()
            if res_json.get("ok"):
                channel_id = res_json["channel"]["id"]
                print(f"Opened Slack DM channel {channel_id} for user {slack_id}")
            else:
                print(f"Warning: conversations.open failed with error: '{res_json.get('error')}'. Falling back to direct user ID.")
        else:
            print(f"Warning: conversations.open returned HTTP {open_res.status_code}. Falling back to direct user ID.")
    except Exception as e:
        print(f"Warning: Exception trying to open conversation: {e}. Falling back to direct user ID.")

    # Post message
    post_url = "https://slack.com/api/chat.postMessage"
    post_payload = {
        "channel": channel_id,
        "text": message
    }
    
    try:
        response = requests.post(post_url, json=post_payload, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"Error: Slack API returned HTTP {response.status_code}")
            return False
        
        res_json = response.json()
        if not res_json.get("ok"):
            print(f"Error: Slack API returned error: '{res_json.get('error')}'")
            return False
            
        print(f"Slack DM successfully sent to {slack_id}")
        return True
    except Exception as e:
        print(f"Error: Exception occurred while sending Slack DM: {e}")
        return False

def create_calendar_event(calendar_service, calendar_id, task_name, assignee_name, slack_id, today_date):
    """
    Creates a 15-minute Google Calendar event for today starting at the current time.
    Checks for duplicate events to prevent multiple creations within the same day.
    """
    if not calendar_id:
        print("Skipping Calendar Event: No CALENDAR_ID provided.")
        return

    # Define the event summary
    summary = f"CRITICAL: {task_name}"

    # Search for existing events today with the same summary to prevent duplication
    time_min = datetime.datetime.combine(today_date, datetime.time.min).replace(tzinfo=datetime.timezone.utc).isoformat()
    time_max = datetime.datetime.combine(today_date, datetime.time.max).replace(tzinfo=datetime.timezone.utc).isoformat()
    
    try:
        events_result = calendar_service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True
        ).execute()
        events = events_result.get("items", [])
        
        if any(e.get("summary") == summary for e in events):
            print(f"Calendar event for '{task_name}' already exists for today. Skipping creation.")
            return
            
    except HttpError as e:
        print(f"Warning: Could not list calendar events to check for duplicates: {e}")

    # Set duration to 15 minutes starting now (UTC)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    start_time = now_utc.isoformat()
    end_time = (now_utc + datetime.timedelta(minutes=15)).isoformat()

    event_body = {
        "summary": summary,
        "description": (
            f"Critical task requires immediate attention.\n\n"
            f"Task: {task_name}\n"
            f"Assignee: {assignee_name}\n"
            f"Slack ID: {slack_id}"
        ),
        "start": {
            "dateTime": start_time,
        },
        "end": {
            "dateTime": end_time,
        },
    }

    try:
        created_event = calendar_service.events().insert(calendarId=calendar_id, body=event_body).execute()
        print(f"Successfully created 15-minute Calendar event: {created_event.get('htmlLink')}")
    except HttpError as e:
        print(f"Error creating Calendar event: {e}")

def main():
    sheet_id = os.environ.get("SHEET_ID")
    calendar_id = os.environ.get("CALENDAR_ID")
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    
    if not sheet_id:
        raise ValueError("SHEET_ID environment variable is not set.")

    # Authenticate and build services
    print("Authenticating with Google APIs...")
    creds = get_credentials()
    gc = gspread.authorize(creds)
    calendar_service = build("calendar", "v3", credentials=creds)

    print(f"Opening Google Sheet: {sheet_id}")
    # Using the first sheet tab
    sh = gc.open_by_key(sheet_id).sheet1
    
    # Retrieve all spreadsheet values
    rows = sh.get_all_values()
    if len(rows) <= 1:
        print("No tasks found in the sheet.")
        return

    # Normalize header formatting
    headers = [h.strip() for h in rows[0]]
    required_cols = ["Task Name", "Task Type", "Assignee Name", "Priority", "Due Date", "Status", "Slack ID"]
    
    col_indices = {}
    for req in required_cols:
        match = None
        # Exact match
        for idx, h in enumerate(headers):
            if h.lower() == req.lower():
                match = idx + 1
                break
        # Substring match fallback
        if not match:
            for idx, h in enumerate(headers):
                if req.lower() in h.lower():
                    match = idx + 1
                    break
        if not match:
            raise ValueError(f"Required column '{req}' could not be matched in spreadsheet headers: {headers}")
        col_indices[req] = match

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    tomorrow_str = (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    print(f"Processing tasks for today's date: {today_str}")

    # Iterate through each row (skip header at row 1)
    for row_idx in range(2, len(rows) + 1):
        row_data = rows[row_idx - 1]
        
        # Normalize row length to match headers
        while len(row_data) < len(headers):
            row_data.append('')

        # Fetch columns values using the discovered indices (subtract 1 for 0-based array index)
        task_name = row_data[col_indices["Task Name"] - 1].strip()
        task_type = row_data[col_indices["Task Type"] - 1].strip()
        assignee_name = row_data[col_indices["Assignee Name"] - 1].strip()
        priority = row_data[col_indices["Priority"] - 1].strip()
        due_date_str = row_data[col_indices["Due Date"] - 1].strip()
        status = row_data[col_indices["Status"] - 1].strip()
        slack_id = row_data[col_indices["Slack ID"] - 1].strip()

        # Skip completely empty task name rows
        if not task_name:
            continue

        # Parse the task due date
        try:
            due_date = datetime.datetime.strptime(due_date_str, "%Y-%m-%d").date()
        except ValueError:
            print(f"Skipping row {row_idx}: Invalid date format '{due_date_str}' for task '{task_name}'. Expected YYYY-MM-DD.")
            continue

        # 1. Send reminders for tasks due today that are Pending
        if due_date == today and status.lower() == "pending":
            print(f"Found Pending task due today: '{task_name}' (Assignee: {assignee_name})")
            
            # Send Slack direct message reminder
            message = (
                f"🔔 *Task Reminder*:\n"
                f"*Task:* {task_name}\n"
                f"*Assignee:* {assignee_name}\n"
                f"*Due Date:* {due_date_str}\n"
                f"*Priority:* {priority}\n\n"
                f"Please update the task status in the sheet when completed!"
            )
            send_slack_dm(slack_id, message, slack_bot_token)

            # If Red Priority, schedule the 15-minute Calendar event
            if priority.lower() == "red":
                print(f"Task '{task_name}' is Priority RED. Setting up Google Calendar event...")
                create_calendar_event(calendar_service, calendar_id, task_name, assignee_name, slack_id, today)

        # 2. Auto-Rollover for Daily tasks marked Done today (or previously due but completed today)
        if task_type.lower() == "daily" and status.lower() == "done" and due_date <= today:
            print(f"Daily task marked Done: '{task_name}'. Executing Rollover...")

            # Construct new row copying attributes, setting tomorrow's date, and status as Pending
            new_row_values = [""] * len(headers)
            for col_name, idx in col_indices.items():
                if col_name == "Due Date":
                    new_row_values[idx - 1] = tomorrow_str
                elif col_name == "Status":
                    new_row_values[idx - 1] = "Pending"
                else:
                    new_row_values[idx - 1] = row_data[idx - 1]

            # Append the rollover task to the sheet
            sh.append_row(new_row_values)
            print(f"Appended new task rollover for '{task_name}' due on {tomorrow_str}")

            # Archive the old task status
            status_col_idx = col_indices["Status"]
            sh.update_cell(row_idx, status_col_idx, "Archived")
            print(f"Successfully archived original Daily task on row {row_idx}")

if __name__ == "__main__":
    main()
