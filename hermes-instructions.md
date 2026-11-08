# Hermes Agent Instructies: cline-hermes-bridge MCP Server

> **Doel:** Deze documentatie beschrijft hoe Hermes Agent kan communiceren met de Cline CLI
> via de `cline-hermes-bridge` MCP-server. Alle voorbeelden en uitleg zijn in het
> Nederlands, ten waar genodigd door technische termen.

---

## 1. Overzicht

**cline-hermes-bridge** is een Middleware MCP (Model Context Protocol) server die Hermes
Agent verbindt met de [Cline CLI](https://github.com/cline/cline). De bridge draait als
een persistente **stdio-server** en stelt Hermes Agent in staat om:

- Cline-sessies te **lijsten** en te **doorzoeken**
- Nieuwe taken te **starten** of bestaande sessies te **hervatten**
- De **runtime-status** van lopende Cline-processen te **monitoren**
- Actieve Cline-processen te **stoppen**

De communicatie verloopt uitsluitend via **stdio** (standard input/output) met behulp van
het **JSON-RPC 2.0** protocol, zoals gedefinieerd door MCP.

---

## 2. Architectuur

```
┌──────────────────┐         stdio (JSON-RPC 2.0)         ┌──────────────────┐
│                  │ ◄──────────────────────────────────► │                  │
│  Hermes Agent    │         MCP Protocol Messages        │ cline-hermes-    │
│                  │                                    │ bridge (stdio)   │
└──────────────────┘                                      └────────┬─────────┘
                                                                       │
                                                                       ▼
                                                          ┌────────────────┐
                                                          │  Cline CLI     │
                                                          │  (subproces)   │
                                                          └────────────────┘
```

### Werking

1. **Hermes Agent** stuurt JSON-RPC 2.0-verzoeken via **stdin** naar de bridge.
2. **cline-hermes-bridge** verwerkt deze verzoeken en communiceert met de Cline CLI
   subprocess via `asyncio.subprocess`.
3. **Antwoorden** worden via **stdout** teruggestuurd naar Hermes Agent.

Alle berichten volgen het JSON-RPC 2.0-formaat. Hermes Agent gebruikt het MCP-client
protocol om tools aan te roepen en resources te lezen.

---

## 3. Beschikbare MCP Tools

De cline-hermes-bridge server exposeert **4 tools**. Hieronder volgt een gedetailleerde
beschrijving van elke tool, inclusief invoer- en uitvoervoorbeelden.

---

### 3.1 `list_cline_sessions`

**Doel:** Lijst alle Cline CLI-sessies, gesorteerd van nieuwst naar oudst.
Retourneert sessiemetadata inclusief initiële prompt, status en PID.

#### Invoer

Geen argumenten vereist.

```json
{
  "name": "list_cline_sessions",
  "arguments": {}
}
```

#### Uitvoer (Voorbeeld)

```json
[
  {
    "session_id": "session_1794143655625_7idx7",
    "initial_task": "Fix the Cline CLI integration...",
    "created_at": "2026-11-08T13:14:15.681Z",
    "status": "running",
    "interactive": true,
    "pid": 51944
  },
  {
    "session_id": "a1b2c3d4-e5f6-7890-abcd-ef0123456789",
    "initial_task": "Refactor the auth module",
    "created_at": "2026-11-07T09:00:00.000Z",
    "status": "completed",
    "interactive": false,
    "pid": null
  }
]
```

| Veld           | Type   | Beschrijving                                        |
|----------------|--------|-----------------------------------------------------|
| `session_id`   | string | Unieke sessie-ID (gebruik voor hervatten).         |
| `initial_task` | string | Sessie-prompt (max 200 karakters, uit sessie-metadata).      |
| `created_at`   | string | ISO-8601 tijdstip van aanmaken.                   |
| `status`       | string | `running`, `completed`, `unknown`, etc.             |
| `interactive`  | boolean | `true` als interactieve modus.                    |
| `pid`          | int\|null | PID van het Cline subprocess (null als gestopt). |

---

### 3.2 `command_cline_agent`

**Doel:** Start een nieuwe Cline CLI-taak of hervat een bestaande sessie.
Dit is een asynchrome operatie — de tool retourneert direct een PID.

#### Invoer

| Naam              | Type   | Vereist | Beschrijving                              |
|-------------------|--------|---------|-------------------------------------------|
| `prompt`          | string | **Ja**  | De instructies/prompt voor Cline.         |
| `resume_session_id` | string | Nee   | Session ID om te hervatten.               |

**Voorbeeld — nieuwe sessie:**

```json
{
  "name": "command_cline_agent",
  "arguments": {
    "prompt": "Refactor the auth module"
  }
}
```

**Voorbeeld — bestaande sessie hervatten:**

```json
{
  "name": "command_cline_agent",
  "arguments": {
    "prompt": "Continue the refactoring",
    "resume_session_id": "session_1794143655625_7idx7"
  }
}
```

#### Uitvoer

```json
{
  "pid": 51944,
  "status": "spawning",
  "prompt_length": 42
}
```

**Fout (geen prompt):**

```json
{"error": "Parameter 'prompt' is required."}
```

**Fout (prompt te lang):**

```json
{"error": "Prompt exceeds maximum length of 4096 characters."}
```

**Fout (cline CLI niet beschikbaar):**

```json
{
  "pid": 12345,
  "status": "error",
  "error": "cline CLI not found in PATH. Install with: npm install -g @anthropic/cline"
}
```

**Fout (starten mislukt na herhalingen):**

```json
{"error": "Failed to start Cline CLI after 5 attempts: <reden>"}
```

**Fout (hervatten met ongeldige sessie-ID):**

```json
{"error": "Session not found"}
```
**Doel:** Controleer of een specifiek Cline CLI-proces actief, gestopt of verlopen is.
Retourneert levensduur, runtime en stdout/stderr fragmenten.

#### Invoer

| Naam  | Type    | Vereist | Beschrijving                      |
|-------|---------|---------|-----------------------------------|
| `pid` | integer | **Ja**  | PID zoals geretourneerd door `command_cline_agent`. |

```json
{
  "name": "get_agent_runtime_status",
  "arguments": {
    "pid": 51944
  }
}
```

#### Uitvoer

```json
{
  "pid": 51944,
  "alive": true,
  "started_at": "2026-11-08T13:14:15.681Z",
  "runtime_seconds": 15.3,
  "stdout_excerpt": "Starting Cline task...\nProcessing...",
  "stderr_excerpt": "",
  "return_code": null
}
```

| Veld               | Type   | Beschrijving                                    |
|---------------------|--------|------------------------------------------------|
| `alive`             | boolean | `true` terwijl het proces nog loopt.         |
| `started_at`        | string | ISO-8601 tijdstip van starten.               |
| `runtime_seconds`   | number |  Verstreken tijd sinds starten.              |
| `stdout_excerpt`    | string | Laatste 1024 tekens van stdout.               |
| `stderr_excerpt`    | string | Laatste 1024 tekens van stderr.                |
| `return_code`       | int\|null | Aftappcode (null zolang lopend).            |

**Fout (niet gevolgd):**

```json
{
  "pid": 99999,
  "alive": false,
  "error": "Process not tracked"
}
```

---

### 3.4 `stop_cline_agent`

**Doel:** Beëindigt een lopende Cline subprocess. Stuurt eerst `SIGTERM`, escalierend
naar `SIGKILL` na 0,5s indien onresponsief.

#### Invoer

| Naam  | Type    | Vereist | Beschrijving                      |
|-------|---------|---------|-----------------------------------|
| `pid` | integer | **Ja**  | PID van te beëindigen proces.     |

```json
{
  "name": "stop_cline_agent",
  "arguments": {
    "pid": 51944
  }
}
```

#### Uitvoer

```json
{
  "pid": 51944,
  "killed": true,
  "status": "terminated"


**Fout (kill-mislukking):**



---

---

## 4. Beschikbare MCP Resources

Naast tools exposeert de bridge ook **read-only resources** voor het lezen van
sessiegeschiedenis en statusinformatie.

---

### `cline://sessions`

Retourneert een JSON-array van alle beschikbare sessies (identiek aan `list_cline_sessions`
tool, maar via resource-URI).

```json
[
  {
    "session_id": "session_1794143655625_7idx7",
    "initial_task": "Fix the Cline CLI integration...",
    "created_at": "2026-11-08T13:14:15.681Z",
    "status": "running",
    "interactive": true,
    "pid": 51944
  }
]
```

---

### `cline://sessions/{session_id}/chat`

Retourneert de volledige chatgeschiedenis voor een specifieke sessie.

```json
{
  "session_id": "a1b2c3d4-...",
  "messages": [
    {
      "role": "user",
      "text": "Refactor the auth module..."
    },
    {
      "role": "assistant",
      "text": "I'll refactor the auth module..."
    }
  ]
}
```

| Veld     | Type   | Beschrijving                           |
|----------|--------|----------------------------------------|
| `role`   | string | `user`, `human`, `assistant`, etc.    |
| `text`   | string | Berichttekst (max 4096 karakters).    |

**Fout (sessie niet gevonden):**

```json
{
  "error": "No session found with id 'invalid-id'"
}
```

---

### `cline://agent/status`

Retourneert de geaggregeerde status van alle door de bridge gevolgde Cline-processen.

```json
{
  "status": "RUNNING",
  "active_processes": 2,
  "pids": [51944, 52001]
}
```

Mogelijke waarden voor `status`: `RUNNING` (minstens één proces actief), `IDLE`
(geen actieve processen).

---

## 5. Gebruiksvoorbeelden

### 5.1 Nieuwe taak starten

```
1. Roep list_cline_sessions aan om te controleren of er geen
   lopende sessies zijn.

2. Roep command_cline_agent aan met je prompt:
   {"name": "command_cline_agent", "arguments": {"prompt": "Write a REST API for user management"}}

3. Bewaar de teruggegeven PID voor latere monitoring.
```

### 5.2 Taak monitoren

```
1. Roep get_agent_runtime_status aan met de PID:
   {"name": "get_agent_runtime_status", "arguments": {"pid": 51944}}

2. Controleer:
   - alive: is het proces nog actief?
   - runtime_seconds: hoe lang draait het?
   - stderr_excerpt: zijn er fouten?

3. Herhaal tot de taak is voltooid (return_code != null).
```

### 5.3 Bestaande sessie hervatten

```
1. Roep list_cline_sessions aan om de juiste session_id te vinden.
   Filter op sessies met status "completed" of "running".

2. Roep command_cline_agent aan met de resume_session_id:
   {"name": "command_cline_agent", "arguments": {
     "prompt": "Add error handling to the API endpoints",
     "resume_session_id": "session_1794143655625_7idx7"
   }}

3. De sessie wordt hervat op het oorspronkelijke punt.
```

### 5.4 Taak stoppen

```
1. Roep stop_cline_agent aan met de PID:
   {"name": "stop_cline_agent", "arguments": {"pid": 51944}}

2. Controleer of killed: true is geretourneerd.

3. Gebruik get_agent_runtime_status om te bevestigen:
   - alive: false
   - return_code: != null
```

### 5.5 Chatgeschiedenis uitlezen

```
1. Gebruik list_cline_sessions om beschikbare session_ids te vinden.

2. Lees de chatgeschiedenis via de resource:
   URI: cline://sessions/{session_id}/chat

3. Parse de returned JSON-array van message-objecten
   (role + text) voor analyse.
```

---

## 6. Configuratie

### 6.1 Hermes config.yaml

Voeg de volgende sectie toe aan `~/.hermes/config.yaml`:

```yaml
cline-hermes-bridge:
  command: python
  args:
    - /Users/wgorter/Development/cline-hermes-bridge/server.py
  env: {}
  disabled: false
  timeout: 300
```

### 6.2 Aangepast Sessiepad

Als je Cline CLI sessies op een ander pad opslaat, stel dan de omgevingsvariabele
`CLINE_HISTORY_DIR` in:

```yaml
cline-hermes-bridge:
  command: python
  args:
    - /Users/wgorter/Development/cline-hermes-bridge/server.py
  env:
    CLINE_HISTORY_DIR: "/pad/naar/mijne/cline/sessions"
  disabled: false
  timeout: 300
```

---

## 7. Belangrijke Opmerkingen

### Sessie-directorystructuur

Cline CLI slaat elke sessie op als een **directory** (geen platte JSON-bestanden):

```
~/.cline/data/sessions/              # macOS / Linux
%APPDATA%\.cline\data\sessions\      # Windows
│
└── <session_id>/                    # De sessie-ID als mapnaam
    ├── <session_id>.json            # Sessiemeta-data
    └── <session_id>.messages.json   # Berichtengeschiedenis
```

| Bestand               | Inhoud                                           |
|-----------------------|--------------------------------------------------|
| `<session_id>.json`   | Metadata: prompt, status, PID, started_at, etc. |
| `<session_id>.messages.json` | Volledige conversatiegeschiedenis.          |

### CLI-commando's

| Commando                            | Beschrijving                                |
|-------------------------------------|---------------------------------------------|
| `cline "prompt"`                    | Start een **nieuwe** sessie.                 |
| `cline --id <id> "prompt"`          | **Hervat** een bestaande sessie.             |
| `cline --help`                      | Toon alle beschikbare opties.                |

De bridge gebruikt deze exacte syntax bij het spawnen van Cline CLI-subprocessen.

### JSON-RPC 2.0-protocol

Alle communicatie met de bridge gebeurt via JSON-RPC 2.0 via MCP.
Hermes Agent hoeft het JSON-RPC-protocol niet handmatig te construeren —
de MCP-client bibliotheek van Hermes doet dit automatisch.

### Foutafhandeling

| Scenario                        | Gedrag                                      |
|---------------------------------|---------------------------------------------|
| Sessie-directory niet aanwezig   | Retourneert lege array `[]`. Geen crash.     |
| Corrupte of onleesbare JSON       | Wordt overgeslagen, stil.                   |
| Cline CLI niet in PATH          | Structured JSON-error van `command_cline_agent`. |
| PID niet gevolgd                 | Retourneert `"error": "Process not tracked"` van `get_agent_runtime_status`. |
| Kill-command mislukt            | Retourneert `"killed": false` met foutmelding. |

Alle fouten worden geretourneerd als **gestructureerde JSON** binnen de MCP `TextContent`
envelop.

### Prestatie-overwegingen

- Het uitlezen van volledige chatgeschiedenis
  (`cline://sessions/{id}/chat`) kan grote responsen genereren.
  Gebruik dit alleen wanneer nodig.
- `get_agent_runtime_status` is lichter dan het lezen van de volledige
  chatgeschiedenis en wordt aanbevolen voor frequente statuscontroles.
- `list_cline_sessions` doorloopt alle sessie-directory's. Bij een groot aantal
  sessies kan dit enkele seconden duren.

---

## Bijlage: Snelle Referentietabel

| Tool / Resource               | Doel                                | Vereiste Argumenten      |
|-------------------------------|-------------------------------------|--------------------------|
| `list_cline_sessions`         | Alle sessies ophalen                | Geen                     |
| `command_cline_agent`         | Nieuwe taak starten / hervatten     | `prompt` (+ `resume_session_id` optie) |
| `get_agent_runtime_status`    | Status van een proces               | `pid`                    |
| `stop_cline_agent`            | Proces beëindigen                   | `pid`                    |
| `cline://sessions`            | Resource: sessielijst               | Geen                     |
| `cline://sessions/{id}/chat`  | Resource: chatgeschiedenis          | `session_id` (in URI)    |
| `cline://agent/status`        | Resource: aggregate status            | Geen                     |

---

*Systeempaden:*

| Component    | Pad                                                        |
|--------------|------------------------------------------------------------|
| Sessions     | `~/.cline/data/sessions/` (macOS/Linux)                    |
| Sessions     | `%APPDATA%\.cline\data\sessions\` (Windows)                |
| Server       | `server.py`                                                |
| Config       | `~/.hermes/config.yaml`                                    |

---

*Laatste bijgewerkt: 2026-11-08 — Code review verbeteringen toegepast*
