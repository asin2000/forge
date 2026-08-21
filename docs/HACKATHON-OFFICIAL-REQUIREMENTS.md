# All Things Agentic Hackathon — Official Requirements (as provided by entrant, 2026-08-21)

> Verbatim capture of the Devpost hackathon page text supplied on 2026-08-21. Reference document only — the FORGE build baseline remains FORGE-REQUIREMENTS.md.

## WHAT TO BUILD

Build and deploy a next-generation, autonomous AI Agent leveraging Gemini 3.5 Flash that operates beyond standard chat loops. The system can run asynchronously in the background, handle the heavy lifting of complex workflows, or dynamically manipulate data pipelines and representations.

Projects must be built within one of these three categories:

**Taskmaster**: Build a complete workflow, not just a chatbot. Don't just make an agent that writes text. Make one that takes action. Find a messy, multi-step chore in your job, classes, or personal life. Build an agent that handles the details, sends the right info to the right places, and proves it can do the heavy lifting for you.

**Collaborative Partner**: Build an agent that leads the way and takes notes. It should ask clarifying questions, guide the user step-by-step, and have a clear way to capture feedback, so it constantly adapts to the user's unique way of thinking.

**Fortified Enterprise Fleet**: Build a scalable network of institutional agents that hook into official enterprise infrastructure. Teams must demonstrate how agents are cataloged for cross-department use, how they safely maintain context across weeks of asynchronous operations, and how they interact with production data without violating enterprise compliance, data sovereignty, or security policies.

- Discovery & Lifecycle: Agent Registry (the central repository for publishing, versioning, and discovering enterprise-approved agents).
- Core Execution & State: Agent Runtime (for long-running, asynchronous background execution) and Memory Bank (for persistent, secure cross-session context over extended timelines).
- Security & Governance: Agent Identity (for zero-trust access control), Agent Gateway (for unified routing and policy enforcement), and Model Armor (inline guardrails to block prompt injection, tool poisoning, and PII leaks).
- Telemetry: Agent Observability (OpenTelemetry-compliant audit logs and end-to-end reasoning chain traces).

Recommended Tech to use (Gemini Enterprise Agent Platform): [the components listed above]

**Every project, in every track, must use:**

- Gemini 3.5 or newer accessed through Gemini API or Vertex AI
- At least one Google Agent Framework: Google ADK, GenAI SDK, Antigravity SDK or GenKit
- At least one Google Cloud infrastructure service (such as Cloud Run, Cloud SQL, Firestore, GKE, Pub/Sub).

Note on cost & deployment: Your app does not need to be publicly accessible or live at the exact moment of submission or judging (so you don't rack up unnecessary costs). You just need to provide clear proof that it was built and deployed on Google Cloud — for example, shown in your demo video and code repository.

## WHAT TO SUBMIT

- Category
- URL to the hosted Project (if available) for judging and testing, such as web UI, Chrome Extension, mobile app, etc. A hosted project is highly encouraged.
- Text description: features and functionality; technologies used; other data sources used; findings and learnings
- URL to your public or private code repository (GitHub, GitLab, or Bitbucket). If your repo is private, share it with testing@devpost.com and cloudhackathons@google.com
- Spin-up Instructions: a step-by-step guide in README.md explaining how to set up and run the project locally or deploy it to the cloud. Even if the judges do not run it, these instructions prove the project is reproducible.
- Architecture Diagram with a clear visual representation of your system (e.g., how Gemini connects to your backend, database, and frontend).
- ~4-min Demo video: short overview of the problem; value proposition; demo of the app in action; must demonstrate the backend is running on Google Cloud (Google Cloud Console, Cloud Run dashboard, Vertex AI logs, URL of .run, etc.)

**For bonus points, optionally one or both:**

- Publish a piece of content (blog, podcast, video) covering how the project was built, on any public platform. Must be public (not unlisted), and must state it was created for the purposes of entering this hackathon.
- Publish a social media post on X, LinkedIn, Instagram, or Facebook with hashtag #AllThingsAgenticHackathon.
- Successfully integrate Google AI models such as Gemma, Veo or Lyria.

## PRIZES ($180,000 total)

- **Grand Prize** — $50,000 + $5,000 GCP credits (1 winner)
- **The Taskmaster** — $20,000 + $2,000 credits (1)
- **The Collaborative Partner** — $20,000 + $2,000 credits (1)
- **The Fortified Enterprise Fleet** — $20,000 + $2,000 credits (1)
- **Startup Excellence** — $20,000 + $5,000 credits (1) — must submit on behalf of an incorporated organization and provide a corporate email address
- **Individual/Hobbyist (Best Team/Solo Build)** — $10,000 + $1,000 credits (2)
- **Best Architectural Design** — $5,000 + $1,000 credits (2)
- **Best Multimodal UX** — $5,000 + $1,000 credits (2)
- **Honorable Mentions** — $2,000 + $500 credits (5)

## JUDGING CRITERIA

- **Innovation & Operational Utility — 40%**: How much real-world friction does the agent remove on its own? Rewards autonomous, high-value action over simple chat — agents that make decisions and complete tasks with little to no hand-holding.
- **Architectural Discipline & Tech Stack — 30%**: How sound are the engineering choices? Decoupled systems, state and memory management, secured credentials, failure handling — robust, production-minded agents, not brittle scripts.
- **Demo & Production Readiness — 30%**: How clearly do the video and repo prove it works? A live, unedited demo, a clean architecture diagram, reproducible setup, and visible proof it runs on Google Cloud.

---

## FULL OFFICIAL RULES — captured from allthingsagentichackathon.devpost.com/rules, 2026-08-21

**Supersedes the bonus description above, which was miscaptured ("one or both" over three bullets). Actual structure below.**

- **Contest/Submission Period (§4):** Aug 3, 2026 09:00 AM PT – Aug 31, 2026 5:00 PM PT. Projects must be **newly created during the Submission Period** (§6); standard tools, frameworks, libraries, and AI coding assistants permitted; any other pre-existing code or work incorporated must be disclosed.
- **Eligibility (§3):** age of majority; ineligible residencies: Italy, Quebec, Crimea, Cuba, Iran, Syria, North Korea, Sudan, Belarus, Russia; not under U.S. export controls/sanctions. Ineligible: employees/interns/contractors/office-holders of Google, Devpost, or involved organizations (plus immediate family/household); persons **employed by a government agency**; anyone whose participation creates a real or apparent conflict of interest. Individual, team, or organization entries allowed; teams/orgs name a Representative.
- **Ownership (§6):** submission must be original work product, **solely owned** by the entrant with no other person or entity having any right or interest, and must not violate any third-party rights. No development with financial or preferential support from Sponsor/Administrator.
- **License grant (§12):** entrant grants Google, subsidiaries, agents, and partners a perpetual, irrevocable, worldwide, royalty-free, non-exclusive license to use, reproduce, adapt, modify, publish, distribute, publicly perform, create derivative works from, and publicly display the Project, for judging and for advertising/promotion.
- **Publicity (§14):** consent to promotion of the submission and use of personal information (name, likeness, photograph, voice, opinions, hometown, country) in any media worldwide without further payment.
- **Prizes (§9, §10):** **each Project is eligible for up to one (1) Prize.** Cash payable to the winner if an individual. Winners responsible for taxes; Sponsor may withhold for tax compliance.
- **Bonus points (§6, §8) — three stacking categories, cumulative max 1.0 point on a 5-point scale (max 6):**
  - Content publication (blog/podcast/video, public, made-for-hackathon statement): up to **0.2**
  - Social media post (#AllThingsAgenticHackathon): up to **0.2**
  - **0.2 per additional Google AI model** integrated (Gemma, Veo, Lyria, …), up to **0.6**
- **Repo & testing access (§6):** private repo must be shared with testing@devpost.com and cloudhackathons@google.com. A private hosted project must include **login credentials in the testing instructions**. Judges are not required to test and may judge solely on the text description, images, and video.
