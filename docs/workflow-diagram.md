# Personal AI Assistant Workflow Map

This document explains how the current product works end to end, mapped by folder, runtime logic, and user-to-output flow.

## 1) Folder-to-Responsibility Map

```mermaid
flowchart TD
    A[Project Root] --> B[app/]
    A --> C[tests/]
    A --> D[webchat.html]
    A --> E[chat_server.py]
    A --> F[Dockerfile + docker-compose.yml]
    A --> G[README.md + .env]

    B --> B1[main.py
API routes + orchestration]
    B --> B2[services/llm_engine.py
intent + LLM extraction]
    B --> B3[services/whatsapp_service.py
signature + outbound WhatsApp]
    B --> B4[workers/celery_app.py
Celery config]
    B --> B5[workers/tasks.py
background tasks + reminders]
    B --> B6[models.py
SQLAlchemy entities]
    B --> B7[schemas.py
request/response models]
    B --> B8[database.py
DB engine/session]
    B --> B9[config.py
env settings]

    C --> C1[test_whatsapp_flow.py
integration-style API tests]
```

## 2) User Interaction Flow (Web Chat to Output)

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as webchat.html
    participant API as FastAPI app/main.py
    participant LLM as services/llm_engine.py
    participant DB as PostgreSQL or in-memory fallback
    participant CAL as Google Calendar API
    participant CEL as Celery/Redis workers

    U->>UI: Types a message
    UI->>API: POST /api/v1/chat
    API->>LLM: route_intent(message)

    alt Intent = ADD_OR_MODIFY_TASK
        API->>LLM: extract_task_details_with_llm(message)
        alt LLM extraction succeeds
            LLM-->>API: title, minutes, priority, deadline, parser_used
        else LLM extraction fails
            API->>API: deterministic fallback parsing
        end

        API->>API: create_task(...)
        API->>CEL: schedule_deadline_reminder(task_id, deadline)
        API->>DB: persist task

        alt Google OAuth token available
            API->>CAL: create calendar event
            CAL-->>API: google_event_id
        else No token/refresh issue
            API-->>API: continue without calendar event
        end

        API-->>UI: reply + intent + confidence + parser_used
    else Intent = GET_TODAY_SCHEDULE
        API->>API: get_today_schedule()
        API-->>UI: summarized today plan
    else Other intents
        API-->>UI: rule/start/progress/general response
    end
```

## 3) Task and Reminder Logic Flow

```mermaid
flowchart TD
    A[Task creation request] --> B[Extract title, priority, minutes, deadline]
    B --> C{Deadline exists?}
    C -- Yes --> D[Schedule Celery reminder
10 minutes before deadline]
    C -- No --> E[Skip deadline reminder]

    D --> F[Save task]
    E --> F[Save task]

    F --> G{Google connected?}
    G -- Yes --> H[Create Google Calendar event]
    G -- No --> I[Return task without calendar sync]

    H --> J[Return created task + event id]
    I --> K[Return created task]
```

## 4) Schedule (Today) Logic

```mermaid
flowchart LR
    A[GET /api/v1/schedule/today] --> B[Load in-memory tasks]
    B --> C[Parse deadline timestamps]
    C --> D[Filter tasks where local date == today]
    D --> E{Any tasks left?}
    E -- No --> F[Return clear no-task message]
    E -- Yes --> G[Sort by priority P1->P4, then deadline]
    G --> H[Build schedule rows: local time, title, priority]
    H --> I[Return schedule_table + assistant_message]
```

## 5) Current Output Paths You Can Expect

- Task-creation message:
  - Output includes task confirmation, estimated minutes, priority, optional schedule time.
  - Output also includes parser_used (gemini/openai/fallback/none) in API JSON.
- Schedule message:
  - Output includes a concise today plan summary from filtered today tasks.
- Calendar sync:
  - If connected, task can sync to Google Calendar.
  - If not connected, task still gets created and user gets reconnect hint.
- Reminders:
  - Deadline reminder is scheduled in Celery for tasks with future deadlines.

## 6) What Is Ready vs Next

Ready now:
- Chat-based task creation
- Intent routing with fallback parsing
- Today schedule filtering and prioritization
- Google Calendar create-on-task flow
- Deadline reminder scheduling
- Regression tests for major flows

Still to add in upcoming phases:
- Task update/delete endpoints in chat and API
- Calendar update/delete synchronization
- Reminder delivery channel output (for example WhatsApp push or in-app notification)
- Persistent planner history and richer daily planning heuristics
