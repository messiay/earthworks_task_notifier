import os
import json
import datetime
from datetime import timezone, timedelta
import requests
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configuration
SLACK_CHANNEL_ID = os.environ['SLACK_CHANNEL_ID']

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

def send_slack_notification(slack_id, message, token):
    """
    Sends a message to the SLACK_CHANNEL_ID and tags the user if slack_id is provided.
    """
    if not token:
        print("Skipping Slack notification: No SLACK_BOT_TOKEN provided.")
        return False
    if not SLACK_CHANNEL_ID:
        print("Skipping Slack notification: No SLACK_CHANNEL_ID provided.")
        return False

    if slack_id:
        clean_id = slack_id.replace("<@", "").replace(">", "").replace("@", "").strip()
        if f"<@{clean_id}>" not in message:
            message = f"<@{clean_id}>\n{message}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }

    # Post message
    post_url = "https://slack.com/api/chat.postMessage"
    post_payload = {
        "channel": SLACK_CHANNEL_ID,
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
            
        print(f"Slack notification successfully sent to channel {SLACK_CHANNEL_ID}")
        return True
    except Exception as e:
        print(f"Error: Exception occurred while sending Slack notification: {e}")
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

    # Evaluate in IST (UTC+5:30)
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.datetime.now(ist)

    # The Morning Alarm
    if now_ist.hour == 9:
        print("Morning Alarm: Sending morning broadcast to Slack.")
        message = "🌅 Good morning team! Please make sure your tasks for today are logged in the master sheet."
        send_slack_notification(None, message, slack_bot_token)
        return

    # Authenticate and build services
    print("Authenticating with Google APIs...")
    creds = get_credentials()
    gc = gspread.authorize(creds)
    calendar_service = build("calendar", "v3", credentials=creds)

    print(f"Opening Google Sheet: {sheet_id}")
    # Using the first sheet tab
    sh = gc.open_by_key(sheet_id).sheet1
    
    # Retrieve all spreadsheet records
    records = sh.get_all_records()
    if not records:
        print("No tasks found in the sheet.")
        return

    # Find the actual dictionary key for each required column dynamically
    sample_keys = list(records[0].keys())
    required_cols = ["Task Name", "Task Type", "Assignee Name", "Priority", "Due Date", "Status", "Slack ID"]
    key_mapping = {}
    for req in required_cols:
        match = None
        for k in sample_keys:
            if k.strip().lower() == req.lower():
                match = k
                break
        if not match:
            for k in sample_keys:
                if req.lower() in k.strip().lower():
                    match = k
                    break
        if not match:
            raise ValueError(f"Required column '{req}' could not be matched in spreadsheet keys: {sample_keys}")
        key_mapping[req] = match

    completion_alert_key = sample_keys[8] if len(sample_keys) >= 9 else None

    today = now_ist.date()
    today_str = today.strftime("%Y-%m-%d")
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    
    print(f"Processing tasks for today's date (IST): {today_str}")

    # Loop through the spreadsheet records
    for idx, record in enumerate(records):
        row_idx = idx + 2  # records are 0-indexed, row 1 is header

        task_name = str(record[key_mapping["Task Name"]]).strip()
        task_type = str(record[key_mapping["Task Type"]]).strip()
        assignee_name = str(record[key_mapping["Assignee Name"]]).strip()
        priority = str(record[key_mapping["Priority"]]).strip()
        due_date_str = str(record[key_mapping["Due Date"]]).strip()
        status = str(record[key_mapping["Status"]]).strip()
        slack_id = str(record[key_mapping["Slack ID"]]).strip()
        completion_alert = str(record.get(completion_alert_key, "")).strip() if completion_alert_key else ""

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
            
            # Send Slack reminder
            message = (
                f"🔔 *Task Reminder*:\n"
                f"*Task:* {task_name}\n"
                f"*Assignee:* {assignee_name}\n"
                f"*Due Date:* {due_date_str}\n"
                f"*Priority:* {priority}\n\n"
                f"Please update the task status in the sheet when completed!"
            )
            send_slack_notification(slack_id, message, slack_bot_token)

            # If Priority is exactly Red, create the Google Calendar event
            if priority == "Red":
                print(f"Task '{task_name}' has Priority exactly Red. Setting up Google Calendar event...")
                create_calendar_event(calendar_service, calendar_id, task_name, assignee_name, slack_id, today)

        # 2. Celebration for Done tasks where Completion Alert is not Sent
        if status.lower() == "done" and completion_alert.lower() != "sent":
            print(f"Found completed task: '{task_name}' (Assignee: {assignee_name})")
            
            # Send Slack notification celebration
            clean_id = slack_id.replace("<@", "").replace(">", "").replace("@", "").strip() if slack_id else ""
            if clean_id:
                celebration_message = f"🎉 *Task Completed!* Awesome job <@{clean_id}> on completing *{task_name}*!"
            else:
                celebration_message = f"🎉 *Task Completed!* Awesome job {assignee_name} on completing *{task_name}*!"
                
            send_slack_notification(slack_id, celebration_message, slack_bot_token)
            
            # Update the 9th column (Column I) to 'Sent'
            sh.update_cell(row_idx, 9, "Sent")
            print(f"Marked completion alert as Sent for row {row_idx}")

            # If it is a Daily task, roll it over to tomorrow (append a new row) and mark the old row's Status column (7th column) as Archived
            if task_type.lower() == "daily":
                new_row_values = []
                for key in sample_keys:
                    if key == key_mapping["Due Date"]:
                        new_row_values.append(tomorrow_str)
                    elif key == key_mapping["Status"]:
                        new_row_values.append("Pending")
                    elif completion_alert_key and key == completion_alert_key:
                        new_row_values.append("")  # Reset completion alert for the new task
                    else:
                        new_row_values.append(str(record.get(key, "")))

                sh.append_row(new_row_values)
                print(f"Appended new task rollover for '{task_name}' due on {tomorrow_str}")

                # Mark the old row's Status column (7th column) as Archived
                sh.update_cell(row_idx, 7, "Archived")
                print(f"Successfully archived original Daily task on row {row_idx}")

if __name__ == "__main__":
    main()
