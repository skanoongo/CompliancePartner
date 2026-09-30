# How Workato evidence is collected

For anyone reviewing or signing off this evidence who does not work on the code.
Two SOX controls are prepared from Workato. Below is what the application does on
its own, and the points where it stops and waits for a person.

**It is read-only.** It navigates and photographs. It never starts, stops, edits,
creates or deletes anything in Workato.

Colours in both diagrams:

| | |
|---|---|
| blue | the application does this |
| amber | a person must do this |
| green | evidence produced |
| red | stops rather than guess |

## Change management — CM-02, monthly

```mermaid
flowchart TD
  A["Reviewer signs in<br/>with Okta"] --> B{"Allowed to work<br/>on Workato?"}
  B -- "no" --> B1["Not offered.<br/>A SOX administrator grants it"]
  B -- "yes" --> C["Chooses the workspace<br/>and presses Prepare"]
  C --> D["Signs in to Workato<br/>credentials from Doppler"]
  D --> E{"Signed in?"}
  E -- "single sign-on<br/>or 2-step needed" --> E1["Waits. A person finishes<br/>sign-in in the browser"]
  E1 --> F
  E -- "yes" --> F["Checks the chosen workspace<br/>is visible on the page"]
  F --> G{"Right workspace?"}
  G -- "no" --> G1["Stops. Nothing is captured"]
  G -- "yes" --> H["Opens every recipe folder<br/>and version history in scope"]
  H --> I["Photographs each page:<br/>web address and clock in every shot"]
  I --> J{"Everything as<br/>the scope expects?"}
  J -- "no" --> J1["Records a warning<br/>for a person to check"]
  J1 --> K
  J -- "yes" --> K["Builds the Excel workbook"]
  K --> L["Reviewer opens the evidence<br/>and completes three checks"]
  L --> M["Management reviews<br/>and signs off"]

  classDef tool fill:#e8eefc,stroke:#2f5fd0,stroke-width:1.5px,color:#16233d
  classDef person fill:#fdf1dc,stroke:#9a5c08,stroke-width:1.5px,color:#3d2a05
  classDef evidence fill:#e2f2ee,stroke:#0b7360,stroke-width:1.5px,color:#07312a
  classDef stop fill:#fae8e8,stroke:#a32f2f,stroke-width:1.5px,color:#3d1010
  classDef ask fill:#ffffff,stroke:#5e6b82,stroke-width:1.3px,color:#18212f

  class A,L,E1,M person
  class C,D,F,H,I tool
  class B,E,G,J ask
  class K evidence
  class G1,B1 stop
  class J1 person
```

Every screenshot is a whole-screen capture, so the web address and the clock appear
in the picture itself — the evidence shows where a page came from and when, not only
what it said.

**The wrong workspace means no evidence at all.** The workspace you chose is
confirmed visible on each page before anything is photographed. Evidence from the
wrong workspace would look completely normal and describe the wrong set of recipes,
so the run stops instead.

**Warnings must be resolved before sign-off.** When a folder cannot be reached, or
holds fewer recipes than the scope says it should, the run records a warning and
carries on. The screenshots are kept and flagged. A clean run and a run with
warnings never look the same.

**A spreadsheet decides what is in scope.** One row per recipe with a Capture Yes/No
column. A recipe found in Workato but missing from the sheet is reported as a
warning — never captured quietly, never skipped quietly.

## User access review — UA-04, quarterly

```mermaid
flowchart TD
  A["Reviewer picks the period<br/>and presses Prepare"] --> B["Signs in and checks<br/>the workspace"]
  B --> C["Opens the first known<br/>collaborator page"]
  C --> D{"Does this page really<br/>list people?"}
  D -- "no" --> E{"Another page<br/>left to try?"}
  E -- "yes" --> C
  E -- "no" --> F["Stops, and lists every page it tried.<br/>Never reports an empty user list"]
  D -- "yes" --> G["Reads each person: name, email,<br/>role, and the status as displayed"]
  G --> H{"Is the status<br/>a known one?"}
  H -- "no" --> H1["Marked unrecognised, with a warning.<br/>Never assumed to have no access"]
  H1 --> I
  H -- "yes" --> I["Sorts into active, inactive<br/>and pending invitation"]
  I --> J["Shows the list on the page, with<br/>a workbook, a spreadsheet and screenshots"]
  J --> K["Reviewer confirms each person's<br/>access is still appropriate"]
  K --> L["Management reviews<br/>and signs off"]

  classDef tool fill:#e8eefc,stroke:#2f5fd0,stroke-width:1.5px,color:#16233d
  classDef person fill:#fdf1dc,stroke:#9a5c08,stroke-width:1.5px,color:#3d2a05
  classDef evidence fill:#e2f2ee,stroke:#0b7360,stroke-width:1.5px,color:#07312a
  classDef stop fill:#fae8e8,stroke:#a32f2f,stroke-width:1.5px,color:#3d1010
  classDef ask fill:#ffffff,stroke:#5e6b82,stroke-width:1.3px,color:#18212f

  class A,K,L person
  class B,C,G,I tool
  class D,E,H ask
  class J evidence
  class F stop
  class H1 person
```

The list is a snapshot taken on the day it runs, labelled with the review period.
Workato does not publish a historical roster, so it evidences access as it stood
when the capture ran.

**An unfamiliar status is never read as "no access".** Workato shows a state per
person — active, invited, suspended, deactivated — worded differently depending on
the plan. The wording is recorded exactly as displayed and the verdict worked out
separately. Anything that cannot be interpreted is flagged for a person, because
under-counting who has access is the one mistake a user access review must not make.

**An empty list is never presented as an answer.** If no page qualifies as a genuine
list of people, the run fails and names every page it opened. "No users found" and
"we could not find the user list" are different statements, and only one of them is
ever made.

## What a completed run hands you

| | |
|---|---|
| Excel workbook | one tab per control area, every screenshot embedded beside its web address and timestamp |
| Screenshots | whole-screen captures, each showing the web address and the clock, downloaded together |
| Capture manifest | `manifest.json` — the address, timestamp and description behind every picture |
| User list | for the access review: `users.csv`, and the same list on screen sorted into active, inactive and pending |

The application organises evidence. It does not reach a conclusion: the three
preparer checks and management sign-off are performed by people, and any warning a
run records has to be resolved before that sign-off.
