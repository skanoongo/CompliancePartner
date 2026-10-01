# Compliance Partner

> **Purpose of this page.** This is the end-user reference for the Compliance Partner application. It is written so that it can also be loaded into a chatbot (any LLM, using retrieval or as a system-prompt knowledge base) that answers user questions about links, roles and processes. Each section is self-contained, and the FAQ at the end is in question/answer form for easy retrieval.
>
> **Source:** documented from the running application at `http://localhost:8080` (Interactive Prototype) on 30 Sep 2026. Replace `http://localhost:8080` with the hosted URL once the app is deployed.

---

## 1. What is Compliance Partner?

Compliance Partner is CoreWeave's SOX compliance workspace. It helps people prepare control workpapers, monitor controls for exceptions, and run internal audit tests across SOX in-scope systems.

Its guiding principle, shown on every page: **"Evidence prepared by the application. Decisions owned by people."** The application gathers evidence and drafts workpapers; a human must always validate the evidence, AI-generated content and conclusions.

The application has four modules:

| Module | Who uses it | What it is for |
|---|---|---|
| Control Preparation | Control Preparers, Admins | Prepare control workpapers (UAR, change management, SOC 1), validate them and send them for management review through Jira |
| Compliance Monitoring | Compliance Monitors, Admins | Run monitoring checks, investigate exceptions, report and export issues |
| Audit Testing | Internal Audit Users, Admins | Set up a test for an RCM control, upload samples and PBC evidence, generate and revise testing workpapers |
| User Administration | Admins | Manage users, roles, module access, assigned systems/controls and the SOX system inventory |

---

## 2. Links and pages

| Page | URL | What you see |
|---|---|---|
| Sign in | `http://localhost:8080/login?next=/` | Sign in with your CoreWeave account (Okta). In local mode a name is requested instead. |
| Choose a system | `http://localhost:8080/choose` | The SOX systems your Okta groups entitle you to. Choosing one opens its workspace. |
| System workspace | `http://localhost:8080/?system=<SystemName>` | The workspace for one system, e.g. `?system=Workato`, `?system=NetSuite` |
| Jira (management review) | `https://coreweave.atlassian.net/jira` | Opened by **Generate Jira ticket**; you create the ticket and attach your workpaper there |

**Direct workspace links (systems on the Choose page):**

| System | Link | Status |
|---|---|---|
| Workato | `http://localhost:8080/?system=Workato` | 2 live controls (Change management, User access review) |
| NetSuite | `http://localhost:8080/?system=NetSuite` | 2 live controls (User access review, Change management) |
| Workday | `http://localhost:8080/?system=Workday` | Prototype only – evidence is simulated |
| Salesforce | `http://localhost:8080/?system=Salesforce` | Prototype only – evidence is simulated |
| Coupa | `http://localhost:8080/?system=Coupa` | Prototype only – evidence is simulated |
| Orderful | `http://localhost:8080/?system=Orderful` | Prototype only – evidence is simulated |
| GitHub | `http://localhost:8080/?system=GitHub` | Prototype only – evidence is simulated |
| Okta | `http://localhost:8080/?system=Okta` | Prototype only – evidence is simulated |
| FloQast | `http://localhost:8080/?system=FloQast` | Prototype only – evidence is simulated |

**Page header controls (inside a workspace):**

- **Guide** – opens "Explore Compliance Partner", a short how-to for every module.
- **Preview As** – an Admin can view the app as another user to check what that user can see.
- **System picker** – switch between systems assigned to you.
- **Module tabs** – Control Preparation · Compliance Monitoring · Audit Testing · User Administration (only the modules your role allows are shown).
- **Reset prototype** – clears demo data in the prototype.

---

## 3. Signing in and access

1. Open the application. You are sent to **Sign in**.
2. Sign in with your **CoreWeave account (Okta)**. Access to each SOX system is granted by your **Okta groups**.
3. After sign-in, **Choose a system** lists only the SOX systems you are entitled to. Choose one to open its workspace.

**Important notes**

- The workspace holds **live, signed-in sessions to the systems under review**, which is why sign-in is required.
- **Local mode** (when Okta is not configured): the app asks only for a name and takes it at face value. It decides which systems you see, not who you are. Okta must be configured in `config/auth.yaml` before the address is reachable by others.
- When sign-in is turned off, every system is listed because nobody is identified.

### Roles

| Role | Modules available |
|---|---|
| Admin | Control Preparation, Compliance Monitoring, Audit Testing, User Administration |
| Control Preparer | Control Preparation |
| Compliance Monitor | Compliance Monitoring |
| Internal Audit User | Audit Testing |

**Visibility rule:** a control is visible only when **both** its scope (system or business process) **and** the control are assigned to you. Enterprise-wide and business-process scopes apply to Audit Testing.

---

## 4. Control Preparation (first line of defense)

**Where:** Workspace → **Control Preparation** tab.
**Purpose:** Prepare control workpapers and validate them before export.

### 4.1 Dashboard

The top of the page shows counts for **Overdue**, **Due Soon** and **Prepared**, then the list of the system's controls with Period, Due Date and Status.

### 4.2 Controls available for each system

| Control | ID | Cadence | Period you select | Due date rule |
|---|---|---|---|---|
| User access review (UAR) | UA-04 | Quarterly | Quarter (Q1–Q4) + fiscal year | Quarter-end |
| Change management | CM-02 | Monthly | Period start and period end dates | Month-end |
| SOC 1 assessment | ELC-14 | Annual / semiannual | "As of" date | By January 30 of the following year |

**How preparation deadlines work:** Red means **overdue**. Amber means **due within 30 days**. Dates follow each control's selected period.

**LIVE CAPTURE badge:** For Workato and NetSuite, UAR and Change management are live. The app really signs in to the system, reads it and screenshots what it reads. Capture is **read-only**: nothing is started, stopped, edited or deleted. Other systems use simulated evidence.

### 4.3 Capture scope (live-capture controls)

Before you prepare, the **Capture Scope** panel shows:

- **Workspace** – the target system workspace (for example *Development*). The capture checks that this workspace is visible on every page before it reads anything, and **stops rather than collect evidence from a different workspace**.
- **Review period** – the period from *Preparation Period*, so it is clear which period the run will be labelled with.
- For Workato, the user listing is a **point-in-time snapshot**. Workato publishes no historical roster.

### 4.4 Step-by-step: prepare a workpaper

The workflow has four stages: **1. Prepare → 2. Human validation → 3. Management review → 4. Sign off.**

1. **Select a control** from the list (UAR, Change management or SOC 1).
2. **Set the Preparation Period** (quarter and fiscal year, start/end dates, or as-of date).
3. Click **Prepare Workpaper**. You can do this at any time. Each run creates a **fresh version** (v1, v2, …), and the activity log records it.
   - Example UAR evidence rows: *User listing* (population reconciled), *Employee / contractor reconciliation* (accounts requiring human disposition).
4. **Human validation.** Review the evidence and tick the acknowledgment:
   > I acknowledge that I am responsible for validating the completeness and accuracy of the source evidence and population used to prepare this control. I must independently validate all AI-generated content and must not rely on it without review.

   The acknowledgment unlocks **Download current workpaper**. It does **not** certify that your review is complete.
5. **Download** the workpaper (an HTML file). Inspect or edit it in your preferred editor. Uploaded HTML is never executed in the application.
6. **Management review & sign-off through Jira.** Click **Generate Jira ticket**. CoreWeave's Jira opens in a new tab (`https://coreweave.atlassian.net/jira`). **Create the ticket yourself and attach the downloaded workpaper.** The button does not create a ticket or upload a file automatically.
7. **Management decision** (reviewer):
   - Download the current workpaper to review or edit.
   - Optionally upload a manager-edited workpaper for return. It becomes current when **Request changes** is selected.
   - Tick *"I reviewed the current workpaper, evidence and conclusions."*
   - Choose **Approve and sign off**, or **Request changes** (a note is required).
   - If changes are requested, the preparer must **validate and resubmit** before approval.
8. **Version history** and **Activity history** are kept per control. Activity history can be exported (Date/Time UTC, Actor, Activity, System, Control, Period, Version, Workpaper filename).

---

## 5. Compliance Monitoring (second line of defense)

**Where:** Workspace → **Compliance Monitoring** tab.
**Purpose:** Monitor your controls and investigate issues that need attention.

### 5.1 Monitoring checks

| Check | What it does |
|---|---|
| Employee & Contractor Termination | Compares termination dates with account disable dates |
| Password Policy Alignment | Compares SOX in-scope system settings with corporate password policy |
| Change Management | Checks every production change for a linked ticket and appropriate management approval before release |

- **Run All Monitoring Checks** runs every check for the selected system.
- Select a check, then click **Run Selected Check** to run only that one.
- Counters show **Open Issues**, **Investigating** and **Closed**.

### 5.2 Issue queue

Issues are listed with Priority, Issue, Control, Status and First Detected. Filter by status (All / Open / Investigating / Closed) and by control. Example issue types:

| Example ID | Issue | Control | Expected | Observed | Priority |
|---|---|---|---|---|---|
| TERM-014 | Employee account remains active | Termination | Disabled within policy deadline | Active after deadline | High |
| TERM-008 | Contractor access needs review | Termination | Access ends with contract | Account remains enabled | High |
| CM-021 | Production release has no linked ticket | Change Management | Linked ticket | No ticket | High |
| CM-022 | Management approval follows production release | Change Management | Approval before release | Approval after release | High |
| AUTH-003 | Local password configuration differs | Password Policy | Match corporate password policy | Differs | Medium |

### 5.3 Step-by-step: investigate an exception

1. In the Issue Queue, click **Investigate →** on an issue.
2. The **Exception investigation** panel shows priority, ID, system, **Expected** and **Observed** values.
3. Validate the source evidence and decide whether the flag is valid.
4. Set **Disposition**: Open, Investigating or Closed.
5. Write an **Investigation / resolution note** documenting remediation or an approved explanation.
6. Tick *"I verified supporting evidence for the disposition."*
7. Click **Save disposition**. A human remains responsible for the decision.

### 5.4 Reports & Export

Click **Reports & Export** to open **Monitoring Reports**:

- **Report type:** Issues or Run History
- **Filters:** System (All or one system), Period start/end, Monitoring controls, Priority (High/Medium/Low), Issue status, Issue search
- **Generate Report**, then **Export CSV**
- Issue dates mean *first detected*. Run dates mean *execution time*. Issue status is the current disposition.

---

## 6. Audit Testing (third line of defense / Internal Audit)

**Where:** Workspace → **Audit Testing** tab.
**Purpose:** Set up your test, organize evidence and prepare a workpaper. Review and sign-off happen in your audit system.

### 6.1 Control catalog (RCM)

The catalog holds **596 controls** in three groups:

- **IT General Controls**, by system (for example Workato 6, NetSuite 9, Okta 6, GitHub 6, Salesforce 8, Workday 7, Coupa 7, Orderful 7, SDLC 8, and others)
- **Entity Level Controls**: Enterprise-wide (27)
- **Business Process**: Revenue (75), Fixed Assets (92), Financial Close and Reporting (48), Procure to Pay – Direct (35), Leases (29), Treasury (28), Debt (25), HR & Payroll (22), Equity (18), Tax (17), Construction (13), Business Combination (11), Procure To Pay – Indirect (10), Variable Interest Entities (9), Joint Ventures (6)

Example Workato ITGCs: C-UA-05 Generic/Shared Accounts Access Review · C-UA-04 Periodic User Access Review · C-UA-01 Password Configuration based on Policy · C-UA-02 New/Modified User Access approval · C-CM-01 Program Change Evaluation/Allocation & Approvals · C-CM-02 Logging and Monitoring of changes. Each control has an RCM identifier (for example `WTO.AS.GSAAR.C-UA-05`).

### 6.2 Step-by-step: run an audit test

1. **Expand an audit category** and choose a control.
2. **Define the test**
   - Testing period: **Over a period** (start and end date) or **Point in time**
   - Assessment: **Design Assessment**, **Operating Effectiveness Testing** or **Both**
   - Testing template: **Use standard template** (the built-in SOX example) or **Upload a new testing template**. Use **Preview Standard Template** to view or edit it. Saved changes apply to future tests of that control in this browser.
3. **Upload population, selected samples and supporting (PBC) evidence.** You can select several files at once. File purposes are suggested from filenames, so review or correct them. One combined file can serve several purposes. Files stay on your device.
4. **Acknowledge preparer responsibility** (the same statement as in Control Preparation).
5. Click **Generate Draft Workpaper**.
6. **Review attributes and supporting evidence.** Save corrections, then **regenerate** before downloading. Download is blocked until corrections are regenerated.
7. **Download Current Workpaper**, or **Upload Revised Workpaper** (an externally revised version becomes the current version; it does not change the standard template). Previous versions are listed.
8. Complete review and sign-off in your audit system.

**Standard template fields:** Control number and name, System, Testing period, Assessment, Sample selection, Evidence (PBC support), Auditor conclusion.

---

## 7. User Administration (Admins only)

**Where:** Workspace → **User Administration** tab.
**Purpose:** Manage roles, module access and assigned controls.

### 7.1 Add or edit a user

1. Click **Add User**, or click **Edit Access** on an existing user.
2. Enter **Full name** and choose a **Role** (Admin, Internal Audit User, Control Preparer, Compliance Monitor).
3. Set **Module Access** (Control Preparation, Compliance Monitoring, Audit Testing).
4. Assign **Systems & Process Scopes**: SOX systems and business processes. **Select All** and **Clear All** are available.
5. Set **Control Access** for Control Preparation, Compliance Monitoring and Audit Testing controls.
6. The **Effective Access** summary shows what the user will see. Click **Save User**.
7. Use **Remove** to delete a user.

### 7.2 SOX system inventory

- Lists the systems people can be assigned to (25 in the prototype).
- **Add a system** to extend the inventory.
- **Removing a system** also removes it from everyone assigned to it.
- A system marked **CAPTURE WIRED** (Workato, NetSuite) cannot be removed, because a live capture reads it and the control would still be offered but could not run.

---

## 8. SOX in-scope systems

1Password, Arena, Argo, Billing TSDB, CoStar, Coupa, Data Lake, Doppler, Equity Edge, FloQast, GitHub, JPMorgan, Kyriba, Linux (OS), NetSuite, Okta, Orderful, Salesforce, Snowflake, Vanta, Wiz, Workato, Workday, Zip, Zuora. Additional audit scopes include OPAL, PagerDuty, Rippling, Teleport, Victoria Metrics and SDLC.

---

## 9. Prototype limitations (current state)

- Data and identities are **fictional or simulated**, except controls marked **Live capture** (read-only).
- **No live Jira connection.** You create tickets manually.
- Uploaded files and revisions stay **in browser memory**. **Refreshing clears files, activity, run history and dispositions.** Download your workpaper and export history before refreshing.
- Roles and scopes simulate access. They are **not production authentication**. User changes last only for the page session.
- AI evidence analysis is **not connected** in Audit Testing. Testing conclusions require your input.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| SOX | Sarbanes-Oxley Act: controls over financial reporting |
| UAR | User Access Review (control UA-04) |
| CM | Change Management (control CM-02) |
| ELC | Entity Level Control (e.g. ELC-14 SOC 1 assessment) |
| ITGC | IT General Control |
| RCM | Risk and Control Matrix: the catalog of controls |
| PBC | Provided By Client: supporting evidence supplied for testing |
| Workpaper | The document that evidences a control was performed or tested |
| Live capture | Read-only automated sign-in, read and screenshot of a source system |
| Disposition | The decision on a monitoring issue: Open, Investigating or Closed |
| Design Assessment / Operating Effectiveness | Whether a control is designed correctly / whether it worked over the period |

---

## 11. FAQ (for chatbot retrieval)

**Q: How do I log in to Compliance Partner?**
A: Open the app and sign in with your CoreWeave (Okta) account. Your Okta groups decide which SOX systems you can see.

**Q: Why can't I see a system or control?**
A: A control is visible only when both its system/process scope and the control itself are assigned to you. Ask an Admin to update your access in User Administration.

**Q: Which module should I use?**
A: Use Control Preparation to prepare a workpaper, Compliance Monitoring to run checks and investigate exceptions, Audit Testing for internal audit testing, and User Administration (Admins only) to manage access.

**Q: When is my UAR due?**
A: At quarter-end. Change management is due at month-end, and SOC 1 by January 30 of the following year. Amber means due within 30 days; red means overdue.

**Q: How do I prepare a User Access Review workpaper?**
A: Open the system, go to Control Preparation, select *User access review (UA-04)*, choose the quarter and fiscal year, click **Prepare Workpaper**, validate the evidence, tick the acknowledgment, download the workpaper, then click **Generate Jira ticket** and attach it to the ticket you create.

**Q: Does "Generate Jira ticket" create the ticket for me?**
A: No. It opens CoreWeave Jira (`https://coreweave.atlassian.net/jira`) in a new tab. You create the ticket and attach the workpaper yourself.

**Q: Why is the Download button disabled?**
A: In Control Preparation you must tick the validation acknowledgment first. In Audit Testing you must generate the workpaper, complete the acknowledgment and regenerate after any corrections.

**Q: What does LIVE CAPTURE mean? Will it change anything in the system?**
A: The app signs in to the system (currently Workato and NetSuite), reads it and screenshots what it reads. It is read-only: nothing is started, stopped, edited or deleted.

**Q: Can I prepare the workpaper more than once?**
A: Yes. Each click on Prepare creates a new version, and version history is kept.

**Q: What happens when a manager requests changes?**
A: The workpaper returns to the preparer, optionally with a manager-edited file that becomes current. The preparer must validate and resubmit before it can be approved.

**Q: How do I close a monitoring issue?**
A: Click **Investigate →**, review expected and observed values, set the disposition to Closed, add a resolution note, confirm you verified the evidence, and click **Save disposition**.

**Q: How do I export monitoring issues?**
A: Click **Reports & Export**, choose Issues or Run History, set the filters, click **Generate Report**, then **Export CSV**.

**Q: Can I use my own audit testing template?**
A: Yes. Choose **Upload a new testing template**, or edit and save the standard template via **Preview Standard Template**.

**Q: Will I lose my work if I refresh?**
A: In the current prototype, yes. Files, activity, run history and dispositions are cleared. Download your workpaper and export history first.

**Q: Who approves and signs off?**
A: Control Preparation: management, through the Jira ticket. Audit Testing: in your audit system.

---

## 12. Using this page to build a chatbot

1. **Knowledge source:** export this page (Confluence → *Export to PDF/Word*, or the Confluence REST API) and load it into your model's retrieval index, or paste it into the system prompt for small models.
2. **Chunking:** chunk by heading (sections 1–12). The FAQ entries work best as one chunk per Q/A.
3. **Suggested system prompt:**

```
You are the Compliance Partner Assistant for CoreWeave employees.
Answer only from the Compliance Partner knowledge base provided.
- Give step-by-step instructions using the exact button and tab names.
- Include the relevant link when the user asks where to go.
- Remind users that humans own all decisions: evidence and AI content must be independently validated.
- If a question is about access, tell the user to contact a Compliance Partner Admin.
- If the answer is not in the knowledge base, say so and suggest the Guide button or the compliance team.
- Never claim that the app creates Jira tickets or changes source systems.
```

4. **Maintenance:** update this page whenever the app changes (new live-capture systems, the hosted URL, Jira integration). The chatbot will pick up the changes on re-index.
