# IncidentPilot — Presentation Script

Spoken-style script, page by page. [square brackets] = stage directions.
Total time: ~12 minutes + Q&A. Before starting: `./run.sh` running,
`./scripts/demo.sh up` done, Slack visible on a second screen/tab.

---

## 1. Opening (1 min) — the problem

"Let me start with a situation every developer knows. It's 3 a.m., production
is down, and someone gets paged. What do they actually do? They open a
terminal and start digging — which pod died, when did it start, what do the
logs say, what changed. That investigation usually takes twenty to forty
minutes, and most of it is just *finding* the information, not fixing
anything.

I built **IncidentPilot** to collapse that whole process. It's an AI-powered
incident response platform for Kubernetes. The idea is simple: the platform
watches the cluster continuously, catches failures in seconds, an AI
investigates them with real cluster access, and fixing is one approved
click — or fully automatic where I've allowed it. Let me show you."

---

## 2. Overview page (2 min) — [open http://localhost:8010]

"This is the home screen. Every card here is one namespace — think of a
namespace as one project or one application in the cluster.

[point at a card] Each card answers the questions an engineer asks first:
Is it healthy? — that's the colored pill. How many pods are running versus
expected? — the big number. How many times has it restarted, how many
incidents in the last 24 hours, how long has it been live, and if something
is broken — what kind of failure, written right there in red.

Two design decisions worth mentioning. First, **critical namespaces always
sort to the top** — at 3 a.m. you shouldn't have to search for the fire.
Second, the page **refreshes itself every ten seconds** — no reload button.

How does this data get here? The console asks the Kubernetes API for live
pod state on every refresh — nothing is cached or stale. And the health
logic is deliberately simple: any crash-looping or broken-image pod makes a
namespace Critical, anything else unhealthy makes it Degraded. I chose
simple rules over a fancy health score because during an incident you need
to *trust* the status — a rule you can explain in one sentence beats a
score no one can explain."

---

## 3. Trigger a live failure (1 min) — [terminal]

"Instead of slides, let me break something for real. I built a demo lab —
a sandbox namespace with a small sample shop app, completely isolated from
real workloads.

[run] `./scripts/demo.sh crashloop --auto 300`

This patches the webshop deployment so it crashes on startup — it simulates
a very common real failure: the app can't reach its database, logs a fatal
error, and exits. Kubernetes keeps restarting it — that's the famous
CrashLoopBackOff.

[switch to Overview] Watch the demo-lab card… within seconds it flips to
red **Critical**, jumps to the first position, and the error name appears.
Nobody refreshed anything. And [switch to Slack] — the alert is already in
Slack, with the pod's logs attached. Detection is fully automatic: the
platform watches the Kubernetes API directly, so it sees a crash on the
first or second restart — seconds, not minutes."

---

## 4. Incident detail + AI investigation (2.5 min) — [click the incident]

"Clicking the incident opens the detail page. The enrichment section is
what the pipeline collected automatically at detection time. The raw alert
JSON is at the bottom for anyone who wants full detail.

But here's the headline feature. [click **Run AI investigation**]

What's happening right now is not a canned answer and not a chatbot
guessing. The AI engine behind this — I call it IncidentChat — has actual
tools: it can run kubectl, fetch pod logs, query Prometheus metrics, and
search Loki log history. It works in a loop, exactly like an engineer: run
a command, read the output, decide the next command. List the pods → see
the crash → pull the dead container's logs → read the actual error → form
a conclusion.

[when the result renders] And here's the answer: root cause, the exact log
lines as evidence, and a suggested fix. Notice it *quotes the real log* —
'redis connection refused' — this came from the cluster seconds ago, not
from training data.

Why the loop design? Because one-shot AI answers about live systems are
guesses. Giving the model tools and letting it verify each step means every
claim is backed by a command it actually ran.

And down here — [point at Actions] — the fix. One button: restart the pod.
Important design rule: **the AI can read everything but change nothing.**
Every change goes through exactly one code path, and a human click is the
trigger. [click Approve] — pod deleted, Kubernetes recreates it fresh."

---

## 5. Logs page (1.5 min) — [namespace → View Logs]

"Every namespace has a log viewer with two tabs. The **Raw logs** tab is
for engineers: terminal look, error lines highlighted red, live refresh
every five seconds, a filter box, and — a small thing that matters a lot —
a 'previous run' checkbox. A crash-looping pod's *current* container is
seconds old and empty; the evidence is in the *dead* one. That checkbox is
the difference between seeing nothing and seeing the actual error.

The **AI Summary** tab is for everyone else. [click ✨ Explain these logs]
It sends the last two hundred lines to the AI and returns a structured
explanation — summary, key events, warnings, errors with likely cause, and
recommended actions — readable in ten seconds by someone who has never
touched Kubernetes. Same data, two audiences."

---

## 6. IncidentChat (2 min) — [open IncidentChat]

"This is the conversational side. It looks like ChatGPT — history sidebar,
conversations I can come back to — but every conversation is grounded in
*this cluster, right now*.

[type] 'Which pods are unhealthy right now?'

While it thinks: the same tool loop runs — it's literally querying the
cluster as we speak. [result renders] And the answer comes back as a real
interface, not a wall of text — summary line first, data tables, colored
status chips.

Follow-ups work naturally — I can just say 'investigate the first one' and
it knows what I mean, because each conversation keeps its context.

One more thing that makes this an operations tool, not just a Q&A toy:
[type] 'restart pod <name> in demo-lab'

Notice what it did NOT do — it didn't execute. It parsed my sentence into
an action and asks for approval first. And this parsing is deliberately
*not* done by the AI — it's exact pattern matching. I never want a language
model hallucinating a pod name into a destructive command. AI for
understanding, deterministic code for actions, human for approval. Every
executed action lands in an audit log."

---

## 7. Auto-Heal (1.5 min) — [namespace page, point at 🩺 toggle]

"Now the autopilot part. On any namespace I can switch on **Auto-Heal**.
When it's on, the platform checks that namespace every thirty seconds, and
if a pod is crash-looping, it restarts it automatically — the same action
as the approve button, just without waiting for me.

But autonomy needs guardrails, so the policy is strict and fully readable:
maximum three automatic restarts per workload per hour — after that it
stops and escalates, flagging 'human needed', because if three restarts
didn't fix it, a fourth won't. It never touches databases. It skips
broken-image errors, because a restart can't fix a wrong image tag — it
flags those for a human instead.

[point at Automated & approved actions table] And everything it does — or
deliberately doesn't do — appears here: heal one-of-three, two-of-three,
escalated. Full transparency. I tested this live: it healed three times,
then escalated with a human-readable reason. That's the philosophy in one
line: **the AI reasons, the rules react, the human decides.**"

---

## 8. Settings (1 min) — [open Settings]

"Last page — and this is what makes it a real product rather than my
personal setup. Everything configurable lives here, no code, no config
files: the cluster name, the AI engine connection, Slack — with a live
'Test connection' that actually calls Slack's API — the alert webhook, and
the whole AI provider section: five providers supported, paste a key, test
it — it fetches the real model list and measures latency — pick a model,
click Activate, and the platform switches its AI brain at runtime.

So handing this to another team is: install the backend with one helm
command, run one script, open Settings, paste two keys. Done."

---

## 9. Recovery + closing (1 min)

"[if the --auto timer fired, show Overview] Meanwhile our broken namespace
healed — the demo recovers itself, the card is green again, and Slack got
the resolution notice.

To sum up what you saw: failures detected in seconds, an AI that
investigates with real cluster access and cites evidence, fixes that are
one approved click, opt-in automation with strict guardrails and a full
audit trail, and zero-code configuration.

The stack, briefly: a Python FastAPI console with a server-rendered UI —
no heavy frontend framework, which keeps it fast and simple; SQLite for
storage; a rule-based alert pipeline for detection, because detection must
never hallucinate; and an LLM-powered engine for investigation, because
that's where judgment lives. Each part does what its technology is best at.

It's open source — github.com/mittal122/incidentpilot — clone it, one helm
install, one script, and configure everything from the Settings page.
Questions?"

---

## Q&A cheat sheet

- **"What if the AI is wrong?"** — It cites the log lines it read, so you
  can verify in seconds. And it can't act on its own — every change needs
  a human click, except Auto-Heal, which is rule-based, not AI-driven.
- **"Why not fully automate everything?"** — Restarts fix maybe half of
  incidents. The other half need config or code changes; automating those
  blindly turns one outage into two. The escalation cap encodes that.
- **"What does it cost to run?"** — The platform is free; the only cost is
  LLM API usage, and only when someone asks for an investigation. Works
  with free-tier providers.
- **"How long to set up?"** — One helm install + one script + paste keys
  in Settings. ~15 minutes on a fresh cluster.
- **"Does it work on our cluster?"** — Any conformant Kubernetes.
  Everything cluster-specific is configured from the Settings page.
- **"Is my cluster data sent to the AI provider?"** — Only what the
  investigation touches (pod names, logs, events) goes to the provider you
  configured. Self-hosted/NIM options keep it in your control.
