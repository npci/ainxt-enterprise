// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from "react";
import { API_BASE as API, apiFetch } from '../config';
import {
  MessageSquare, Bot, Database, FolderKanban, DollarSign, ChevronRight, Building2, Shield, Cpu, BarChart2,
  Users, HardDrive, AlertTriangle, BookOpen, Layers, Activity,
  Target, Briefcase, BookMarked, MessagesSquare, Bell, Hammer,
  FlaskConical, Lightbulb, Globe, UserCircle, Mail, Brain, Plug,
  Monitor,
} from "lucide-react";

// ── Module documentation ──────────────────────────────────────
//
// One entry per item in the left sidebar, in the same order, so this panel
// stays a complete description of the product surface rather than a subset.
// It drifted to 9 of 27 features once, and documented four screens that had
// already been removed — hence the ordering convention and the check below.
//
// `desktopOnly` marks features that need the AiNxt Desktop application: they
// drive a local CLI process and touch the filesystem, which a browser cannot.
// `beta` mirrors the sidebar's own flag.
//
// Examples are deliberately domain-neutral. This panel ships in a public
// release, so it should read the same to a bank, a hospital and a games studio.

const MODULES = [
  {
    id: "chat",
    icon: MessageSquare,
    label: "Chat",
    what: "Retrieval-augmented chat over your indexed codebases and documents, with automatic model routing per question.",
    why: "Getting an answer out of a large codebase or document set normally means knowing where to look first. Chat removes that step, and cites what it used.",
    who: "Everyone. It is the default entry point to the platform and needs no configuration.",
    when: "When you need to understand unfamiliar code, find where something is configured, or get an explanation with references.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "How does the retry logic in the payment worker decide to give up?",
        "Where is the session timeout configured, and what is the default?",
        "Explain this class and what calls it.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Show every place we read or write customer identifiers.",
        "Which endpoints skip the authentication dependency?",
        "What compliance checks run before a model call?",
      ]},
      { dept: "Operations", icon: HardDrive, examples: [
        "How do I restart this service without dropping in-flight work?",
        "What does this error code mean and where is it raised?",
      ]},
    ],
  },
  {
    id: "office",
    icon: Briefcase,
    label: "Buddy",
    desktopOnly: true,
    beta: true,
    what: "An AI office assistant that reads your documents, drafts content, produces Word/Excel/PowerPoint files, and acts through connectors such as mail, calendar, chat and issue trackers.",
    why: "Most work is not code. Buddy covers the document-and-inbox half of the job, and runs on your own machine so it can use local files.",
    who: "Non-engineering roles above all — operations, finance, HR, legal, programme management.",
    when: "When the task is to read, summarise, draft or file something, rather than to change code.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Summarise this week's incident reports into a one-page update.",
        "Turn these meeting notes into a formatted status deck.",
        "Find the three attachments I was sent about the vendor review.",
      ]},
      { dept: "HR", icon: Users, examples: [
        "Draft a policy summary from the full handbook, one page, plain language.",
        "Prepare an onboarding checklist as a spreadsheet.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Compare these two contract versions and list the substantive changes.",
        "Extract every dated commitment from this agreement into a table.",
      ]},
    ],
    steps: [
      "Install the AiNxt Desktop application — Buddy is not available in the browser.",
      "Open Buddy Setup and grant access to the folder you want it to work in.",
      "Connect the services you need under Connectors (mail, calendar, drive, issue tracker).",
      "Describe the outcome you want, not the steps. Buddy asks before any write or send.",
      "Generated files land in the folder you granted; nothing is written outside it.",
    ],
  },
  {
    id: "cowork",
    icon: Cpu,
    label: "Code",
    desktopOnly: true,
    beta: true,
    what: "A local coding agent that opens a repository on your own machine, reads and edits files, runs shell commands, and streams every tool call and diff back to you.",
    why: "Reviewing a change is far easier than describing one. Code works in your real working copy, so you can inspect and keep or discard each edit.",
    who: "Engineers, and anyone comfortable reviewing a diff before accepting it.",
    when: "When the work is a concrete change to code you have checked out locally.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Add validation to this endpoint and a test that proves it rejects bad input.",
        "This test is flaky — find the race and fix it.",
        "Migrate this module off the deprecated client and run the suite.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Find hard-coded credentials in this repository and replace them with config lookups.",
        "Add the missing authorisation check to every route in this router.",
      ]},
    ],
    steps: [
      "Install the AiNxt Desktop application and make sure the AiNxt CLI is present — the app will offer to install it if not.",
      "Open Code and choose the local repository to work in.",
      "State the change you want. Every file write and command asks permission first.",
      "Review the streamed diff, then keep or discard it. Nothing is committed for you.",
    ],
  },
  {
    id: "agents",
    icon: Bot,
    label: "Agents",
    what: "Build and run purpose-built agents with their own system prompt, tools, skills and model preference.",
    why: "A repeatable task deserves a repeatable operator. An agent captures the instructions once instead of being re-explained in every chat.",
    who: "Platform engineers and team leads who own a recurring process.",
    when: "When you find yourself pasting the same long instructions more than a few times.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "A review agent that checks pull requests against your own coding standards.",
        "A triage agent that labels and routes incoming bug reports.",
        "A release-notes agent that summarises merged changes.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "A scanner agent that flags newly committed secrets.",
        "A classifier agent that grades incoming alerts by severity.",
      ]},
      { dept: "Operations", icon: HardDrive, examples: [
        "An on-call agent that assembles the first-response context for an incident.",
      ]},
    ],
  },
  {
    id: "knowledge",
    icon: BookOpen,
    label: "Knowledge Base",
    what: "Upload documents (PDF, DOCX, PPTX, MD, HTML, TXT) into a vector index and query them with a scope picker that narrows retrieval deterministically.",
    why: "Chat is only as good as what it can retrieve. This is where the corpus comes from, with visible parse → chunk → embed → save progress instead of a silent upload.",
    who: "Anyone with reference material worth asking questions about; approvers who gate what enters a shared namespace.",
    when: "Before you expect Chat to answer from your own documents rather than from code.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Index the runbooks so on-call can ask instead of grepping a wiki.",
        "Load supplier contracts into a scoped namespace for the procurement team.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Index the regulation set, then ask which internal policy covers a clause.",
      ]},
      { dept: "Engineering", icon: Cpu, examples: [
        "Add design documents so architecture questions cite the decision record.",
      ]},
    ],
    steps: [
      "Open Knowledge Base and drag in the documents, or browse to them.",
      "Pick the namespace and department scope — this controls who can retrieve it.",
      "Watch the parse → chunk → embed → save stages; a compliance block stops the upload and says why.",
      "Switch to KB Chat and use the scope picker to narrow to a domain, product, version or single document.",
    ],
  },
  {
    id: "coach",
    icon: Target,
    label: "AiNxt Coach",
    beta: true,
    what: "A personal dashboard that scores how you use the platform across six practice categories and suggests concrete improvements.",
    why: "Nobody is taught what a good prompt looks like. Coach turns events the gateway already records into feedback, with no extra data collection.",
    who: "Every user for their own dashboard; managers for anonymised team aggregates; compliance for anti-pattern visibility.",
    when: "After a few days of normal use. Weekly is a sensible review cadence.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "See which practice categories are dragging your score down.",
        "Find where a premium model was used for a trivial question, and route those locally.",
        "Rewrite a vague prompt using the suggested version and compare results.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Get alerted when a prompt trips secret or personal-data detection.",
        "Review anti-pattern hotspots across departments without seeing prompt text.",
      ]},
      { dept: "Operations", icon: HardDrive, examples: [
        "Check that the Coach consumer is keeping up with event volume.",
      ]},
    ],
  },
  {
    id: "products",
    icon: FolderKanban,
    label: "Products",
    what: "The product and department registry: which products exist, who owns them, and which teams they belong to.",
    why: "Almost everything else scopes by product or department — retrieval namespaces, budgets, governance, analytics. This is where that structure is defined.",
    who: "Administrators and product owners.",
    when: "During setup, and whenever teams or ownership change.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Register a new product line and assign its owning department.",
        "Reassign ownership after a team reorganisation.",
      ]},
      { dept: "Engineering", icon: Cpu, examples: [
        "Check which product a codebase is filed under before scoping a query.",
      ]},
    ],
  },
  {
    id: "codebase",
    icon: Database,
    label: "Codebase",
    what: "Connect and index repositories so Chat, Agents and CodeWiki can retrieve from them.",
    why: "Indexing is the prerequisite for every code-aware feature. Doing it here once makes it available everywhere.",
    who: "Engineering leads and platform engineers.",
    when: "When onboarding a repository, or after a large refactor makes the index stale.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Add a repository and index its default branch.",
        "Re-index after a migration so retrieval stops citing deleted files.",
        "Check indexing status and chunk counts when answers look thin.",
      ]},
      { dept: "Operations", icon: HardDrive, examples: [
        "Watch index freshness across repositories and schedule refreshes.",
      ]},
    ],
  },
  {
    id: "codewiki",
    icon: BookMarked,
    label: "CodeWiki",
    what: "Generated, browsable documentation for an indexed repository — a module tree with a page per significant component.",
    why: "New joiners need orientation before they need answers. CodeWiki gives a structure to read rather than a prompt to guess at.",
    who: "New joiners, reviewers, and anyone inheriting unfamiliar code.",
    when: "Right after indexing a repository, and before your first deep question about it.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Read the generated overview to learn a service's shape before changing it.",
        "Search page contents for a concept when you do not know the file name.",
        "Regenerate after significant structural change.",
      ]},
      { dept: "HR", icon: Users, examples: [
        "Point a new engineer at the wiki for their team's repository on day one.",
      ]},
    ],
  },
  {
    id: "projects",
    icon: FolderKanban,
    label: "My Workspace",
    what: "Your own saved work: chats worth keeping, generated documents, agent runs and files, in one place.",
    why: "Useful output otherwise disappears into scrollback. Workspace is the difference between a session and a record.",
    who: "Every user, for their own material.",
    when: "Whenever a result is worth returning to, or sharing.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Keep the investigation thread that explained a subtle bug.",
        "Retrieve a generated report you produced last month.",
      ]},
      { dept: "Operations", icon: HardDrive, examples: [
        "Collect the artefacts for an incident review in one folder.",
      ]},
    ],
  },
  {
    id: "discussions",
    icon: MessagesSquare,
    label: "Discussions",
    what: "An internal question-and-answer forum with voting, accepted answers and badges, plus an optional @AiNxt bot that can be mentioned for an AI reply.",
    why: "Some answers should be written down once and found by the next person. Chat is private; this is shared and searchable.",
    who: "Everyone. Especially useful where the same question reaches several people.",
    when: "When an answer has value beyond your own session.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Ask a question the whole team hits, and accept the answer that resolves it.",
        "Mention @AiNxt for a first draft answer, then correct it in a reply.",
      ]},
      { dept: "HR", icon: Users, examples: [
        "Answer recurring policy questions once, publicly.",
      ]},
    ],
  },
  {
    id: "inbox",
    icon: Bell,
    label: "Inbox",
    what: "Platform notifications: approvals waiting on you, job completions, digests and alerts.",
    why: "Long-running work and approval gates need somewhere to land that is not email.",
    who: "Everyone; approvers and administrators see the most traffic.",
    when: "Check it when something you started should have finished, or when you are in an approval path.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Pick up a knowledge-base upload waiting for approval.",
        "See that an overnight generation job finished, and open the result.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Receive an alert when a prompt trips a detection rule.",
      ]},
    ],
  },
  {
    id: "sdlc",
    icon: Layers,
    label: "SDLC Pipeline",
    beta: true,
    what: "Multi-step delivery pipelines — feature work, bug fixes, pull-request review and governance checks — with human approval gates that can resume where they paused.",
    why: "Real delivery is a sequence with checkpoints, not one prompt. The pipeline makes each step explicit and reviewable.",
    who: "Engineering leads and delivery managers.",
    when: "When a task has enough steps that you would otherwise track it by hand.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Take a ticket from description to branch, change and test run, pausing for review.",
        "Run a structured review pass over a pull request before a human looks at it.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Require a governance check to pass before a change can proceed.",
      ]},
    ],
  },
  {
    id: "build-studio",
    icon: Hammer,
    label: "Agent Studio",
    what: "A visual canvas for building multi-agent workflows. Drag agent, condition, loop and subflow nodes onto the canvas, wire them together, and run the result — execution streams live to the browser.",
    why: "A multi-step automation is easier to reason about as a graph than as a long prompt. Studio makes each stage explicit, lets you add human approval gates, and keeps the logic in one place.",
    who: "Anyone designing a repeatable multi-step process. Use the Workflow Factory or Agent Factory chat to generate a first draft from a plain-language description if you prefer not to start from a blank canvas.",
    when: "When a task has more than one stage, needs to branch on a condition, loop over a list, pause for human approval, or run on a schedule or webhook trigger.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Chain a diff-retrieval agent into a standards-check agent and post the result as a GitLab review comment.",
        "Fan a security scan out across multiple repositories in parallel and file a consolidated Jira ticket.",
        "Schedule a release-notes workflow that summarises merged MRs and emails a formatted report.",
      ]},
      { dept: "Operations", icon: HardDrive, examples: [
        "Build a daily incident-digest workflow: triage overnight alerts, draft a briefing deck, send it via Teams.",
        "Trigger a post-deploy check from a signed webhook and post results to a channel.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Extract clauses from uploaded contracts, check them against a policy Knowledge Base, and flag deviations.",
        "Route documents through a stricter review branch when the compliance engine detects sensitive content.",
      ]},
    ],
    steps: [
      "Open Agent Studio and click New Workflow, or describe your automation in the Workflow Factory chat to get a generated starting point.",
      "Drag nodes onto the canvas: Agent for LLM steps, Condition for branching, Loop for iteration, Evaluation Gate for quality checks, Subflow to embed an existing workflow.",
      "Click any Agent node to configure its prompt, model, tools (Jira, GitLab, GitHub, Microsoft 365), skills (PPTX, DOCX, XLSX, PDF) and Knowledge Base.",
      "Wire nodes by dragging from an output handle to the next node's input. Run the workflow and watch each step stream live in the Chat Panel.",
      "Add a Trigger to run automatically — scheduled (daily, weekly, custom cron) or via a signed webhook — then submit for governance approval when ready.",
    ],
  },
  {
    id: "monitoring",
    icon: Activity,
    label: "Monitoring",
    what: "Live platform health: service checks, queue depth, worker state, circuit breakers and error rates.",
    why: "When answers stop arriving, the question is which dependency is down. This answers it directly.",
    who: "Operations and platform engineers.",
    when: "During an incident, and as a routine check after deploying.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Confirm which dependency is failing when requests degrade.",
        "Watch queue depth to decide whether to add workers.",
        "See which circuit breakers are open and why.",
      ]},
      { dept: "Monitoring", icon: BarChart2, examples: [
        "Scrape the metrics endpoint into your own dashboards and alerts.",
      ]},
    ],
  },
  {
    id: "analytics",
    icon: BarChart2,
    label: "Analytics",
    what: "Usage and cost analytics across users, departments, products and models.",
    why: "Spend and adoption questions arrive monthly. This produces the numbers without an export step.",
    who: "Administrators, department heads, finance.",
    when: "At review time, and whenever cost moves unexpectedly.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Break spend down by department and model for the last month.",
        "Find which workloads drove an unexpected increase.",
      ]},
      { dept: "Monitoring", icon: BarChart2, examples: [
        "Track adoption per team to see where enablement is needed.",
      ]},
    ],
  },
  {
    id: "evals",
    icon: FlaskConical,
    label: "Eval Observatory",
    what: "Quality monitoring for AI output — run evaluations, track scores over time, and compare across models and prompt versions.",
    why: "Model and prompt changes need evidence, not impressions. Evals give a number you can compare before and after.",
    who: "Platform engineers and anyone tuning prompts or changing models.",
    when: "Before adopting a model change, and continuously for workloads that matter.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Compare two prompt versions on the same evaluation set.",
        "Check whether a cheaper model holds quality for a specific task.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Show that output quality has not regressed since the last change.",
      ]},
    ],
  },
  {
    id: "model-governance",
    icon: Shield,
    label: "Model Governance",
    what: "Control which models each user, department or product is allowed to reach.",
    why: "Not every workload should be able to call every provider. Governance makes that a policy rather than a convention.",
    who: "Administrators and compliance owners.",
    when: "During setup, and whenever a provider or data-residency rule changes.",
    useCases: [
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Restrict a department to locally hosted models only.",
        "Block a provider outright for workloads touching sensitive data.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Review who currently has access to which providers.",
      ]},
    ],
  },
  {
    id: "skill-proposals",
    icon: Lightbulb,
    label: "Skill Proposals",
    what: "Review queue for skills the platform synthesised itself after noticing the same successful sequence repeat.",
    why: "The system can spot a pattern, but should not promote itself. This is the human gate between observation and a reusable skill.",
    who: "Administrators and approvers.",
    when: "Periodically, as proposals accumulate.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Read the representative prompt and observed tool sequence, then promote or reject.",
        "Check how often a pattern occurred before trusting it.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Confirm provenance and department scope before a skill becomes shared.",
      ]},
    ],
  },
  {
    id: "endpoint-manager",
    icon: Globe,
    label: "Endpoints",
    what: "Create and manage named API endpoints and their access, so a team or external system can call the platform programmatically.",
    why: "Integrations need a credential and a URL that can be issued, scoped and revoked without touching a user account.",
    who: "Administrators and integration owners.",
    when: "When wiring another system into the platform, or rotating access.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Issue an endpoint for a team's own service to call.",
        "Revoke access for a decommissioned integration.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Audit which endpoints exist, who owns them and what they can reach.",
      ]},
    ],
  },
  {
    id: "budget",
    icon: DollarSign,
    label: "Budget",
    what: "Spend limits and current consumption per user, department and product, enforced at request time.",
    why: "A cap that is only reported after the fact is not a cap. Budget refuses the call once the limit is reached.",
    who: "Administrators, department heads, finance.",
    when: "At setup, then whenever limits need adjusting.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Set a monthly ceiling per department and see consumption against it.",
        "Raise a limit temporarily for a project with a deadline.",
      ]},
      { dept: "Risk", icon: AlertTriangle, examples: [
        "Prove that spend cannot exceed an approved figure.",
      ]},
    ],
  },
  {
    id: "level-overrides",
    icon: UserCircle,
    label: "Level Overrides",
    what: "Grant or revoke a temporary elevation of a user's access level.",
    why: "Cover and short-term delegation are normal. Doing it here leaves a record and an expiry, unlike editing a role directly.",
    who: "Directors and administrators.",
    when: "For leave cover, incident response, or a time-boxed project.",
    useCases: [
      { dept: "HR", icon: Users, examples: [
        "Elevate a stand-in for a colleague on leave, with an end date.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Review active elevations and revoke any that are no longer needed.",
      ]},
    ],
  },
  {
    id: "broadcast",
    icon: Mail,
    label: "Email Broadcast",
    what: "Compose and send an announcement to a selected audience, with a preview before it goes out.",
    why: "Platform changes need announcing to the people affected, chosen by the same department structure everything else uses.",
    who: "Administrators and communications owners.",
    when: "For releases, planned downtime, and policy changes.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Announce a maintenance window to the affected departments only.",
      ]},
      { dept: "HR", icon: Users, examples: [
        "Send an enablement note to teams with low adoption.",
      ]},
    ],
  },
  {
    id: "memory",
    icon: Brain,
    label: "Memory",
    beta: true,
    what: "What the platform remembers about you across sessions, and the controls to inspect or clear it.",
    why: "Continuity is useful and needs to be visible. Memory that cannot be reviewed or deleted is a liability rather than a feature.",
    who: "Every user for their own memory; administrators for scope and retention policy.",
    when: "When answers reference stale context, or when you want to know what is retained.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Review remembered context and remove what has gone out of date.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Confirm that sensitive material is classified and not retained beyond policy.",
      ]},
    ],
  },
  {
    id: "connectors",
    icon: Plug,
    label: "Connectors",
    beta: true,
    what: "Governed access to external systems — mail, calendar, drive, chat, issue trackers, document signing and generic HTTP services — usable by agents and by Buddy.",
    why: "Agents should not each learn a provider's API. Connectors give one authorisation, rate-limit and confirmation path for all of them.",
    who: "Administrators to enable and scope; every user to connect their own account.",
    when: "Before asking an agent to read from or write to a system outside the platform.",
    useCases: [
      { dept: "Operations", icon: HardDrive, examples: [
        "Connect mail and calendar so Buddy can find an attachment or check availability.",
        "Connect the issue tracker so an agent can file and update tickets.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Review the scopes each connector requested, and revoke one.",
        "Confirm that every write action requires explicit confirmation first.",
      ]},
    ],
    steps: [
      "An administrator enables the connector and supplies the provider's client credentials.",
      "Open Connectors and authorise your own account — you grant only the listed scopes.",
      "Reads become available immediately; writes and sends always ask you to confirm.",
      "Revoke at any time from the same screen; stored tokens are deleted.",
    ],
  },
  {
    id: "cowork-setup",
    icon: Briefcase,
    label: "Buddy Setup",
    desktopOnly: true,
    beta: true,
    what: "The desktop-side configuration for Buddy and Code: which folder the agent may use, which local tools are permitted, and how the CLI is launched.",
    why: "A local agent needs an explicit boundary. This is where you set it, rather than discovering it by accident.",
    who: "Anyone using Buddy or Code on the desktop application.",
    when: "Once at first run, and whenever you want to change the granted folder or permissions.",
    useCases: [
      { dept: "Engineering", icon: Cpu, examples: [
        "Grant a project folder and confirm the agent cannot read outside it.",
        "Check that the AiNxt CLI is detected, and install it if it is missing.",
      ]},
      { dept: "Security", icon: Shield, examples: [
        "Review which local capabilities are enabled and turn off the ones you do not want.",
      ]},
    ],
  },
];

const DEPT_ICONS = {
  "Engineering":   Cpu,
  "Security":      Shield,
  "HR":            Users,
  "Operations":    HardDrive,
  "Risk":          AlertTriangle,
  "Monitoring":    BarChart2,
};

// ─────────────────────────────────────────────────────────────

function Badges({ m, compact = false }) {
  if (!m.desktopOnly && !m.beta) return null;
  return (
    <span className="flex items-center gap-1 flex-shrink-0">
      {m.desktopOnly && (
        <span
          title="Requires the AiNxt Desktop application"
          className="inline-flex items-center gap-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 px-1 text-[9px] font-semibold uppercase tracking-wide"
        >
          <Monitor size={8} />
          {compact ? "" : "Desktop"}
        </span>
      )}
      {m.beta && (
        <span className="rounded bg-gray-100 text-gray-500 border border-gray-200 px-1 text-[9px] font-semibold uppercase tracking-wide">
          Beta
        </span>
      )}
    </span>
  );
}

export default function DocsPanel() {
  const [selected, setSelected] = useState(MODULES[0]);
  const [expandedDept, setExpandedDept] = useState(null);
  const [uiConfig, setUiConfig] = useState({ internal_use_only: false });
  useEffect(() => {
    apiFetch(`${API}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUiConfig(d); })
      .catch(() => {});
  }, []);

  return (
    <div className="flex h-screen bg-white overflow-hidden">

      {/* ── Left nav ─────────────────────────────────────────── */}
      <div className="w-56 border-r border-gray-200 flex flex-col overflow-y-auto bg-gray-50">
        <div className="px-4 py-4 border-b border-gray-200">
          <div className="flex items-center gap-2">
            <BookOpen size={15} className="text-indigo-700" />
            <span className="text-sm font-semibold  text-indigo-700">Documentation</span>
          </div>
          <p className="text-xs text-gray-400 mt-1">{MODULES.length} features</p>
        </div>

        <nav className="flex-1 py-2">
          {MODULES.map(m => {
            const Icon = m.icon;
            const active = selected.id === m.id;
            return (
              <button
                key={m.id}
                onClick={() => { setSelected(m); setExpandedDept(null); }}
                className={`w-full flex items-center gap-2.5 px-4 py-2 text-sm text-left transition cursor-pointer my-1 ${
                  active
                    ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-l-indigo-500 rounded"
                    : "text-gray-500 hover:text-gray-600 hover:bg-gray-100 rounded"
                }`}
              >
                <Icon size={14} className="flex-shrink-0" />
                <span className="flex-1 truncate">{m.label}</span>
                <Badges m={m} compact />
              </button>
            );
          })}
        </nav>
      </div>

      {/* ── Content ──────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto px-8 py-8 max-w-4xl">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-9 h-9 bg-indigo-50 rounded-lg flex items-center justify-center">
            <selected.icon size={18} className="text-indigo-700" />
          </div>
          <div>
            <h1 className="text-xl font-semibold  text-indigo-700 flex items-center gap-2">
              {selected.label}
              <Badges m={selected} />
            </h1>
            <p className="text-xs text-gray-400">
              {selected.desktopOnly
                ? "Available in the AiNxt Desktop application only"
                : "Platform module documentation"}
            </p>
          </div>
        </div>

        {/* Overview cards */}
        <div className="grid grid-cols-2 gap-4 mb-8">
          {[
            { label: "What it is",  text: selected.what  },
            { label: "Why it exists", text: selected.why  },
            { label: "Who should use it", text: selected.who  },
            { label: "When to use it", text: selected.when },
          ].map(({ label, text }) => (
            <div key={label} className="border border-gray-200 rounded-lg p-4 hover:bg-indigo-50 hover:shadow-xs">
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">{label}</div>
              <p className="text-sm text-gray-700 leading-relaxed">{text}</p>
            </div>
          ))}
        </div>

        {/* Use cases by department */}
        <div>
          <h2 className="text-sm font-semibold text-gray-800 mb-4 flex items-center gap-2">
            <Building2 size={14} />
            Example uses by team
          </h2>

          <div className="space-y-3">
            {selected.useCases.map((uc) => {
              const DeptIcon = DEPT_ICONS[uc.dept] || Building2;
              const isOpen = expandedDept === uc.dept;
              return (
                <div key={uc.dept} className="border border-gray-200 rounded-lg overflow-hidden">
                  <button
                    onClick={() => setExpandedDept(isOpen ? null : uc.dept)}
                    className="w-full flex items-center justify-between px-4 py-3 bg-indigo-50 hover:bg-indigo-50 transition cursor-pointer"
                  >
                    <div className="flex items-center gap-2.5">
                      <DeptIcon size={14} className="text-indigo-500" />
                      <span className="text-sm font-medium text-gray-800">{uc.dept}</span>
                      <span className="text-xs text-gray-400">{uc.examples.length} examples</span>
                    </div>
                    <ChevronRight
                      size={14}
                      className={`text-indigo-400 transition-transform ${isOpen ? "rotate-90" : ""}`}
                    />
                  </button>

                  {isOpen && (
                    <ul className="px-4 py-3 space-y-2">
                      {uc.examples.map((ex, i) => (
                        <li key={i} className="flex items-start gap-2 text-sm text-gray-700">
                          <span className="text-indigo-600 mt-0.5 flex-shrink-0">›</span>
                          <span>{ex}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Step-by-step guide (for modules that define steps) */}
        {selected.steps && (
          <div className="mt-8">
            <h2 className="text-sm font-semibold text-gray-800 mb-4 flex items-center gap-2">
              <ChevronRight size={14} />
              How to use it — step by step
            </h2>
            <ol className="space-y-2">
              {selected.steps.map((step, i) => (
                <li key={i} className="flex items-start gap-3 text-sm text-gray-700 bg-gray-50 rounded-lg px-4 py-3 border border-gray-200">
                  <span className="flex-shrink-0 w-5 h-5 rounded-full bg-indigo-600 text-white text-xs flex items-center justify-center font-semibold mt-0.5">
                    {i + 1}
                  </span>
                  <span className="leading-relaxed">{step.replace(/^\d+\.\s*/, "")}</span>
                </li>
              ))}
            </ol>
          </div>
        )}

        {/* Model routing info for Chat */}
        {selected.id === "chat" && (
          <div className="mt-8 border border-gray-200 rounded-lg p-5">
            <h3 className="text-sm font-semibold text-gray-800 mb-1">Model routing</h3>
            <p className="text-xs text-gray-400 mb-3">
              How Chat picks a model by question complexity. These are the shipped defaults —
              a deployment can change every tier, and Model Governance can restrict them further.
            </p>
            <div className="space-y-2">
              {[
                { tier: "Simple",  model: "Local model (Ollama)", desc: "Greetings and short factual queries — no external call", color: "bg-green-50 text-green-700 border-green-200" },
                { tier: "Medium",  model: "Cloud, mid tier",      desc: "Code, reasoning, agent steps", color: "bg-blue-50 text-blue-700 border-blue-200" },
                { tier: "Complex", model: "Cloud, top tier",      desc: "Architecture and deep analysis", color: "bg-purple-50 text-purple-700 border-purple-200" },
                { tier: "Vision",  model: "Cloud, multimodal",    desc: "Images, diagrams and screenshots", color: "bg-orange-50 text-orange-700 border-orange-200" },
              ].map(({ tier, model, desc, color }) => (
                <div key={tier} className={`flex items-center gap-3 px-3 py-2 rounded-md border text-xs ${color}`}>
                  <span className="font-semibold w-16">{tier}</span>
                  <span className="font-mono font-medium w-44">{model}</span>
                  <span className="text-gray-500">{desc}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="mt-8 pt-4 border-t border-gray-100 text-xs text-gray-400 text-center">
          {uiConfig.internal_use_only ? "AiNxt · Agentic Engineering Platform · Internal Use Only" : "AiNxt · Agentic Engineering Platform"}
        </div>

      </div>
    </div>
  );
}
