# Roles and reviewed experience

Use `telos_roles` once when assigning jobs to learn configured templates. Built-ins
are `designer`, `developer`, and `tester`. Pass `role_profile` on a meeting
participant, project task, or `telos_open`. Meeting `role` still states that
participant's concrete focus. Choose the smallest useful team; roles do not
require every task to involve all three jobs. Existing meeting exceptions apply.

Templates provide responsibilities, workflow, skill needs, deliverables and public
review focus. Adapt the task's private checks/questions to its actual risks before
dispatch. Check the required tools are available, especially browser/visual
evidence for design work. Roles grant no permissions or acceptance authority.
Task paths, dependencies, sandbox and concurrency limits remain unchanged.

A configured role's default executor profile applies only when no explicit
provider, model or executor profile was supplied. Honor explicit user choices.
Queued tasks snapshot the role and executor at creation; later template edits
apply to future tasks. Reassign executors through the existing project flow;
never open a replacement to bypass a budget or no-progress guard.

Give each task/meeting participant precise `experience_tags` for its problem and
technical prerequisites, for example `numeric-input`, `python`, or
`duplicate-submit`. A shared lesson is selected only if **all** of its
`required_tags` match. Read its applicability and limitations before adopting it.
Do not add tags merely to obtain more lessons. Job relevance affects ranking,
not exclusive ownership: designers, programmers and testers can share a lesson.

Legacy repository experience still works without roles or tags. New tagged tasks
combine relevant repository records with reviewed shared summaries, at most three
records and 6000 characters in total. The frozen `experience_refs` record what was
actually supplied. No update is pushed into an already running task in this release.

After acceptance, record concise public lessons via `telos_grade(public_lessons=...)`.
Project children become reusable only after final project acceptance. Then, when
an accepted lesson is useful beyond that repository:

1. Search the source repository with `telos_experience_search` and inspect the
   accepted source. Candidate records are for investigation, not publication.
2. As the original source supervisor, call `telos_experience_publish` with the
   source experience ID, a generalized lesson and a rationale for sharing it.
   Include `problem`, `cause` (use unknown if unconfirmed), `resolution`,
   `applicability`, `limitations`, `required_tags`, and relevant `roles`.
3. Base the summary on reviewed evidence. Exclude project-specific business rules,
   paths, private questions/references/checks and credentials. A new summary needs
   an actual review, not automatic copying of a successful task's text.

This is a local shared notebook, not a model-training mechanism. Multiple original
supervisors using the same Telos state directory can reuse published summaries.
Source evidence stays private. Publishing invokes no model and changes no source
record, active task or acceptance rule. Duplicate publication is idempotent;
disabled lessons remain disabled. Retrieval revalidates supporting acceptance.

Use `telos_experience_forget` to disable an incorrect or outdated repository/shared
record. A disabled or invalid source no longer supports derived shared lessons;
a shared lesson with another valid reviewed source can still remain usable.
Every receiving task must pass its own checks even when a previous task passed.

Configuration: `roles_enabled=false` disables new role assignments;
`shared_experience_enabled=false` disables shared publication/retrieval;
`experience_enabled=false` disables automatic experience injection.
Historical tasks and snapshots are preserved. No cross-device synchronization
or live-update notification is provided in this release.
