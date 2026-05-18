import React from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertCircle,
  BarChart3,
  Bot,
  CalendarClock,
  CheckCircle2,
  Database,
  ExternalLink,
  Headphones,
  Mic,
  MicOff,
  Phone,
  PhoneCall,
  PhoneOff,
  RefreshCcw,
  Search,
  Send,
  Server,
  ShieldCheck,
  Users,
  Webhook
} from "lucide-react";
import { RetellWebClient } from "retell-client-js-sdk";
import "./styles.css";

type IconComponent = React.ElementType<{ size?: number }>;

type Summary = {
  total_leads: number;
  pending_jobs: number;
  in_progress_jobs: number;
  completed_jobs: number;
  failed_jobs: number;
  calls_made: number;
  answered: number;
  hot_leads: number;
  conversion_rate: number;
  webhook_backlog: number;
  crm_failures: number;
};

type Lead = {
  id: string;
  zoho_lead_id: string;
  name: string;
  phone: string;
  email?: string | null;
  city?: string | null;
  language_preference: string;
  source?: string | null;
  campaign?: string | null;
  created_at?: string | null;
  latest_call_job_status?: string | null;
  latest_call_job_id?: string | null;
  latest_attempt_status?: string | null;
  latest_interest_level?: string | null;
};

type CallJob = {
  id: string;
  lead_name?: string | null;
  phone?: string | null;
  status: string;
  scheduled_at?: string | null;
  retry_count: number;
  max_retries: number;
};

type CallAttempt = {
  id: string;
  lead_name?: string | null;
  phone?: string | null;
  retell_call_id: string;
  attempt_number: number;
  status: string;
  recording_url?: string | null;
  summary?: string | null;
  interest_level?: string | null;
  follow_up_required?: boolean | null;
  duration_seconds?: number | null;
  started_at?: string | null;
};

type WebhookEvent = {
  id: string;
  source: string;
  event_type: string;
  processed: boolean;
  idempotency_key: string;
  received_at?: string | null;
};

type Followup = {
  id: string;
  lead_name?: string | null;
  scheduled_at?: string | null;
  zoho_task_id?: string | null;
  status: string;
};

type CrmSyncLog = {
  id: string;
  lead_name?: string | null;
  operation: string;
  success: boolean;
  error_message?: string | null;
  synced_at?: string | null;
};

type Health = {
  status: string;
  environment: string;
};

type TabKey = "overview" | "leads" | "jobs" | "attempts" | "webhooks" | "followups" | "sync";

const tabs: Array<{ key: TabKey; label: string; icon: IconComponent }> = [
  { key: "overview", label: "Overview", icon: BarChart3 },
  { key: "leads", label: "Leads", icon: Users },
  { key: "jobs", label: "Call Jobs", icon: Phone },
  { key: "attempts", label: "Attempts", icon: Headphones },
  { key: "webhooks", label: "Webhooks", icon: Webhook },
  { key: "followups", label: "Follow-ups", icon: CalendarClock },
  { key: "sync", label: "CRM Sync", icon: RefreshCcw }
];

const fmtDate = (value?: string | null) => {
  if (!value) return "-";
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(value));
};

const shortId = (value?: string | null) => {
  if (!value) return "-";
  return value.length > 14 ? `${value.slice(0, 8)}...${value.slice(-4)}` : value;
};

const statusClass = (value?: string | null) => {
  const status = (value || "").toLowerCase();
  if (["completed", "created", "success", "processed", "hot"].includes(status)) return "good";
  if (["pending", "initiated", "ringing", "warm"].includes(status)) return "wait";
  if (["failed", "cancelled", "not interested"].includes(status)) return "bad";
  if (["in_progress", "answered", "cold"].includes(status)) return "info";
  return "neutral";
};

function useDashboardData() {
  const [summary, setSummary] = React.useState<Summary | null>(null);
  const [health, setHealth] = React.useState<Health | null>(null);
  const [leads, setLeads] = React.useState<Lead[]>([]);
  const [jobs, setJobs] = React.useState<CallJob[]>([]);
  const [attempts, setAttempts] = React.useState<CallAttempt[]>([]);
  const [webhooks, setWebhooks] = React.useState<WebhookEvent[]>([]);
  const [followups, setFollowups] = React.useState<Followup[]>([]);
  const [syncLogs, setSyncLogs] = React.useState<CrmSyncLog[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [syncing, setSyncing] = React.useState(false);
  const [lastSyncedAt, setLastSyncedAt] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSyncing(true);
      const syncRes = await fetch("/admin/zoho/sync", { method: "POST" });
      if (!syncRes.ok) throw new Error(`/admin/zoho/sync returned ${syncRes.status}`);
      setLastSyncedAt(new Date().toISOString());

      const [
        healthRes,
        summaryRes,
        leadsRes,
        jobsRes,
        attemptsRes,
        webhooksRes,
        followupsRes,
        syncLogsRes
      ] = await Promise.all([
        fetch("/health"),
        fetch("/admin/summary"),
        fetch("/admin/leads"),
        fetch("/admin/call-jobs"),
        fetch("/admin/call-attempts"),
        fetch("/admin/webhook-events"),
        fetch("/admin/followups"),
        fetch("/admin/crm-sync-logs")
      ]);

      for (const response of [
        healthRes,
        summaryRes,
        leadsRes,
        jobsRes,
        attemptsRes,
        webhooksRes,
        followupsRes,
        syncLogsRes
      ]) {
        if (!response.ok) throw new Error(`${response.url} returned ${response.status}`);
      }

      setHealth(await healthRes.json());
      setSummary(await summaryRes.json());
      setLeads(await leadsRes.json());
      setJobs(await jobsRes.json());
      setAttempts(await attemptsRes.json());
      setWebhooks(await webhooksRes.json());
      setFollowups(await followupsRes.json());
      setSyncLogs(await syncLogsRes.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
    } finally {
      setSyncing(false);
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
    const id = window.setInterval(() => {
      void load();
    }, 30000);
    return () => window.clearInterval(id);
  }, [load]);

  return {
    summary,
    health,
    leads,
    jobs,
    attempts,
    webhooks,
    followups,
    syncLogs,
    loading,
    syncing,
    lastSyncedAt,
    error,
    load
  };
}

// ─── Call Modal ─────────────────────────────────────────────────────────────

type CallState = "idle" | "choosing" | "connecting" | "live" | "ended";

const retellClient = new RetellWebClient();

function CallModal({ lead, onClose }: { lead: Lead; onClose: () => void }) {
  const [callState, setCallState] = React.useState<CallState>("choosing");
  const [error, setError] = React.useState<string | null>(null);
  const [isMuted, setIsMuted] = React.useState(false);
  const [activeMode, setActiveMode] = React.useState<"ai" | "human" | "exotel" | null>(null);

  const cleanup = React.useCallback(() => {
    try { retellClient.stopCall(); } catch {}
  }, []);

  React.useEffect(() => {
    retellClient.on("call_started", () => setCallState("live"));
    retellClient.on("call_ended", () => setCallState("ended"));
    retellClient.on("error", (err) => {
      setError(String(err));
      setCallState("ended");
    });
    return cleanup;
  }, [cleanup]);

  const initiateAI = async () => {
    setActiveMode("ai");
    setCallState("connecting");
    setError(null);
    try {
      const res = await fetch(`/admin/leads/${lead.id}/call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "ai" })
      });
      if (!res.ok) throw new Error(await res.text());
      setCallState("live");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to queue AI call");
      setCallState("choosing");
    }
  };

  const initiateHuman = async () => {
    setActiveMode("human");
    setCallState("connecting");
    setError(null);
    try {
      const res = await fetch(`/admin/leads/${lead.id}/call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "human" })
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      await retellClient.startCall({ accessToken: data.access_token });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start call");
      setCallState("choosing");
    }
  };

  const initiateExotel = async () => {
    setActiveMode("exotel");
    setCallState("connecting");
    setError(null);
    try {
      const res = await fetch(`/admin/leads/${lead.id}/call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "exotel" })
      });
      if (!res.ok) throw new Error(await res.text());
      setCallState("ended");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start Exotel call");
      setCallState("choosing");
    }
  };

  const hangup = () => {
    cleanup();
    setCallState("ended");
  };

  const toggleMute = () => {
    const next = !isMuted;
    setIsMuted(next);
    if (next) {
      retellClient.mute();
    } else {
      retellClient.unmute();
    }
  };

  return (
    <div className="modalOverlay" onClick={onClose}>
      <div className="modalCard" onClick={(e) => e.stopPropagation()}>
        <div className="modalHeader">
          <div className="modalLeadInfo">
            <div className="modalAvatar"><Phone size={20} /></div>
            <div>
              <strong>{lead.name}</strong>
              <span>{lead.phone}</span>
            </div>
          </div>
          <button className="modalClose" onClick={onClose} title="Close">✕</button>
        </div>

        {error && (
          <div className="callError">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}

        {callState === "choosing" && (
          <div className="callOptions">
            <p className="callHint">How would you like to call this lead?</p>
            <button className="callOption aiOption" id="btn-ai-talk" onClick={() => void initiateAI()}>
              <div className="callOptionIcon"><Bot size={28} /></div>
              <div>
                <strong>Let AI Talk</strong>
                <span>Retell AI agent calls the lead and qualifies them automatically</span>
              </div>
            </button>
            <button className="callOption humanOption" id="btn-human-talk" onClick={() => void initiateHuman()}>
              <div className="callOptionIcon"><Mic size={28} /></div>
              <div>
                <strong>Let Me Talk</strong>
                <span>Connect you directly to the lead via browser call</span>
              </div>
            </button>
            <button className="callOption exotelOption" id="btn-exotel-call" onClick={() => void initiateExotel()}>
              <div className="callOptionIcon"><PhoneCall size={28} /></div>
              <div>
                <strong>Call via Exotel</strong>
                <span>Exotel places the call using the configured ExoML app</span>
              </div>
            </button>
          </div>
        )}

        {callState === "connecting" && (
          <div className="callStatus">
            <div className="pulseRing" />
            <p>
              {activeMode === "ai"
                ? "Dispatching AI agent..."
                : activeMode === "exotel"
                  ? "Requesting Exotel call..."
                  : "Connecting your microphone..."}
            </p>
          </div>
        )}

        {callState === "live" && activeMode === "ai" && (
          <div className="callStatus">
            <div className="liveDot" />
            <p>AI agent is calling <strong>{lead.name}</strong></p>
            <small>Call dispatched — check Call Jobs for status updates</small>
          </div>
        )}

        {callState === "live" && activeMode === "human" && (
          <div className="callActive">
            <div className="callActiveDot" />
            <p>Live call with <strong>{lead.name}</strong></p>
            <div className="callControls">
              <button className={`callCtrlBtn ${isMuted ? "mutedBtn" : ""}`} onClick={toggleMute} title={isMuted ? "Unmute" : "Mute"}>
                {isMuted ? <MicOff size={18} /> : <Mic size={18} />}
                <span>{isMuted ? "Unmute" : "Mute"}</span>
              </button>
              <button className="callCtrlBtn hangupBtn" onClick={hangup} title="End call">
                <PhoneOff size={18} />
                <span>Hang Up</span>
              </button>
            </div>
          </div>
        )}

        {callState === "ended" && (
          <div className="callStatus">
            <CheckCircle2 size={40} color="#047857" />
            <p>
              {activeMode === "ai"
                ? "AI call queued successfully"
                : activeMode === "exotel"
                  ? "Exotel call request sent"
                  : "Call ended"}
            </p>
            <button className="tableAction" style={{ marginTop: 8 }} onClick={onClose}>Close</button>
          </div>
        )}
      </div>
    </div>
  );
}

function App() {
  const [activeTab, setActiveTab] = React.useState<TabKey>("overview");
  const [query, setQuery] = React.useState("");
  const [callingLead, setCallingLead] = React.useState<Lead | null>(null);
  const data = useDashboardData();

  const filteredLeads = data.leads.filter((lead) =>
    [lead.name, lead.phone, lead.zoho_lead_id, lead.campaign, lead.city]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(query.toLowerCase())
  );

  return (
    <div className="shell">
      {callingLead && <CallModal lead={callingLead} onClose={() => setCallingLead(null)} />}
      <aside className="sidebar">
        <div className="brand">
          <div className="brandMark"><Activity size={22} /></div>
          <div>
            <h1>LeadCaller</h1>
            <p>AI qualification ops</p>
          </div>
        </div>

        <nav className="nav">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                className={activeTab === tab.key ? "navItem active" : "navItem"}
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                title={tab.label}
              >
                <Icon size={18} />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="systemPanel">
          <div className="systemRow">
            <Server size={16} />
            <span>API</span>
            <b>{data.health?.status || "..."}</b>
          </div>
          <div className="systemRow">
            <ShieldCheck size={16} />
            <span>Env</span>
            <b>{data.health?.environment || "..."}</b>
          </div>
          <div className="systemRow">
            <Database size={16} />
            <span>DB</span>
            <b>Supabase</b>
          </div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <p className="eyebrow">Operations Dashboard</p>
            <h2>{tabs.find((tab) => tab.key === activeTab)?.label}</h2>
          </div>
          <div className="topActions">
            <label className="search">
              <Search size={16} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search leads, phones, campaigns"
              />
            </label>
            <button className="iconButton" onClick={data.load} title="Refresh dashboard data">
              <RefreshCcw size={18} />
            </button>
          </div>
        </header>

        <div className="syncStrip">
          <span>{data.syncing ? "Syncing Zoho CRM..." : "Zoho CRM sync is automatic"}</span>
          <b>Every 30s</b>
          <span>Last sync: {data.lastSyncedAt ? fmtDate(data.lastSyncedAt) : "-"}</span>
        </div>

        {data.error && (
          <div className="notice badNotice">
            <AlertCircle size={18} />
            <span>{data.error}</span>
          </div>
        )}

        {activeTab === "overview" && <Overview summary={data.summary} loading={data.loading} />}
        {activeTab === "leads" && <LeadsTable leads={filteredLeads} onCallLead={setCallingLead} />}
        {activeTab === "jobs" && <JobsTable jobs={data.jobs} onRefresh={data.load} />}
        {activeTab === "attempts" && <AttemptsTable attempts={data.attempts} />}
        {activeTab === "webhooks" && <WebhooksTable webhooks={data.webhooks} />}
        {activeTab === "followups" && <FollowupsTable followups={data.followups} />}
        {activeTab === "sync" && <SyncLogsTable logs={data.syncLogs} />}
      </main>
    </div>
  );
}

function Overview({ summary, loading }: { summary: Summary | null; loading: boolean }) {
  const cards: Array<[string, string | number | undefined, IconComponent]> = [
    ["Total Leads", summary?.total_leads, Users],
    ["Calls Made", summary?.calls_made, Phone],
    ["Answered", summary?.answered, Headphones],
    ["Hot Leads", summary?.hot_leads, CheckCircle2],
    ["Conversion", `${summary?.conversion_rate ?? 0}%`, BarChart3],
    ["Webhook Backlog", summary?.webhook_backlog, Webhook],
    ["CRM Failures", summary?.crm_failures, AlertCircle],
    ["Pending Jobs", summary?.pending_jobs, CalendarClock]
  ];

  return (
    <section className="content">
      <div className="metrics">
        {cards.map(([label, value, Icon]) => (
          <div className="metric" key={String(label)}>
            <div className="metricIcon"><Icon size={20} /></div>
            <span>{label}</span>
            <strong>{loading ? "..." : value}</strong>
          </div>
        ))}
      </div>

      <div className="split">
        <div className="panel">
          <h3>Call Pipeline</h3>
          <div className="pipeline">
            <PipelineItem label="Pending" value={summary?.pending_jobs ?? 0} tone="wait" />
            <PipelineItem label="In Progress" value={summary?.in_progress_jobs ?? 0} tone="info" />
            <PipelineItem label="Completed" value={summary?.completed_jobs ?? 0} tone="good" />
            <PipelineItem label="Failed" value={summary?.failed_jobs ?? 0} tone="bad" />
          </div>
        </div>

        <div className="panel">
          <h3>Service Links</h3>
          <div className="links">
            <a href="https://crm.zoho.com" target="_blank" rel="noreferrer">Zoho CRM <ExternalLink size={14} /></a>
            <a href="https://dashboard.retellai.com" target="_blank" rel="noreferrer">Retell Dashboard <ExternalLink size={14} /></a>
            <a href="https://supabase.com/dashboard" target="_blank" rel="noreferrer">Supabase <ExternalLink size={14} /></a>
            <a href="/docs" target="_blank" rel="noreferrer">FastAPI Docs <ExternalLink size={14} /></a>
          </div>
        </div>
      </div>
    </section>
  );
}

function PipelineItem({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`pipelineItem ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Badge({ value }: { value?: string | null }) {
  return <span className={`badge ${statusClass(value)}`}>{value || "-"}</span>;
}

function EmptyRow({ colSpan }: { colSpan: number }) {
  return (
    <tr>
      <td className="empty" colSpan={colSpan}>No records yet</td>
    </tr>
  );
}

function LeadsTable({ leads, onCallLead }: { leads: Lead[]; onCallLead: (lead: Lead) => void }) {
  return (
    <Table title="Leads" count={leads.length}>
      <thead>
        <tr>
          <th>Name</th>
          <th>Phone</th>
          <th>Campaign</th>
          <th>Language</th>
          <th>Call Status</th>
          <th>Intent</th>
          <th>Created</th>
          <th>Call</th>
        </tr>
      </thead>
      <tbody>
        {leads.length === 0 && <EmptyRow colSpan={8} />}
        {leads.map((lead) => (
          <tr key={lead.id}>
            <td>
              <strong>{lead.name}</strong>
              <small>{shortId(lead.zoho_lead_id)}</small>
            </td>
            <td>{lead.phone}</td>
            <td>{lead.campaign || "-"}</td>
            <td>{lead.language_preference}</td>
            <td><Badge value={lead.latest_call_job_status || lead.latest_attempt_status} /></td>
            <td><Badge value={lead.latest_interest_level} /></td>
            <td>{fmtDate(lead.created_at)}</td>
            <td>
              <button
                id={`call-lead-${lead.id}`}
                className="callLeadBtn"
                onClick={() => onCallLead(lead)}
                title={`Call ${lead.name}`}
              >
                <Phone size={15} />
                <span>Call</span>
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function JobsTable({ jobs, onRefresh }: { jobs: CallJob[]; onRefresh: () => void }) {
  const [busyId, setBusyId] = React.useState<string | null>(null);

  const trigger = async (id: string) => {
    setBusyId(id);
    try {
      const response = await fetch(`/admin/call-jobs/${id}/trigger`, { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
      await onRefresh();
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Table title="Call Jobs" count={jobs.length}>
      <thead>
        <tr>
          <th>Lead</th>
          <th>Phone</th>
          <th>Status</th>
          <th>Scheduled</th>
          <th>Retries</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {jobs.length === 0 && <EmptyRow colSpan={6} />}
        {jobs.map((job) => (
          <tr key={job.id}>
            <td>
              <strong>{job.lead_name || "-"}</strong>
              <small>{shortId(job.id)}</small>
            </td>
            <td>{job.phone || "-"}</td>
            <td><Badge value={job.status} /></td>
            <td>{fmtDate(job.scheduled_at)}</td>
            <td>{job.retry_count}/{job.max_retries}</td>
            <td>
              <button
                className="tableAction"
                disabled={job.status !== "pending" || busyId === job.id}
                onClick={() => void trigger(job.id)}
                title="Trigger this pending Retell call"
              >
                <Send size={15} />
                <span>{busyId === job.id ? "Queueing" : "Trigger"}</span>
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function AttemptsTable({ attempts }: { attempts: CallAttempt[] }) {
  return (
    <Table title="Call Attempts" count={attempts.length}>
      <thead>
        <tr>
          <th>Lead</th>
          <th>Status</th>
          <th>Intent</th>
          <th>Duration</th>
          <th>Summary</th>
          <th>Recording</th>
        </tr>
      </thead>
      <tbody>
        {attempts.length === 0 && <EmptyRow colSpan={6} />}
        {attempts.map((attempt) => (
          <tr key={attempt.id}>
            <td>
              <strong>{attempt.lead_name || "-"}</strong>
              <small>{attempt.phone || shortId(attempt.retell_call_id)}</small>
            </td>
            <td><Badge value={attempt.status} /></td>
            <td><Badge value={attempt.interest_level} /></td>
            <td>{attempt.duration_seconds ? `${attempt.duration_seconds}s` : "-"}</td>
            <td className="summaryCell">{attempt.summary || "-"}</td>
            <td>
              {attempt.recording_url ? (
                <a className="inlineLink" href={attempt.recording_url} target="_blank" rel="noreferrer">
                  Open <ExternalLink size={13} />
                </a>
              ) : "-"}
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function WebhooksTable({ webhooks }: { webhooks: WebhookEvent[] }) {
  return (
    <Table title="Webhook Events" count={webhooks.length}>
      <thead>
        <tr>
          <th>Source</th>
          <th>Type</th>
          <th>Processed</th>
          <th>Idempotency</th>
          <th>Received</th>
        </tr>
      </thead>
      <tbody>
        {webhooks.length === 0 && <EmptyRow colSpan={5} />}
        {webhooks.map((event) => (
          <tr key={event.id}>
            <td><Badge value={event.source} /></td>
            <td>{event.event_type}</td>
            <td><Badge value={event.processed ? "processed" : "pending"} /></td>
            <td>{shortId(event.idempotency_key)}</td>
            <td>{fmtDate(event.received_at)}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function FollowupsTable({ followups }: { followups: Followup[] }) {
  return (
    <Table title="Follow-ups" count={followups.length}>
      <thead>
        <tr>
          <th>Lead</th>
          <th>Scheduled</th>
          <th>Zoho Task</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {followups.length === 0 && <EmptyRow colSpan={4} />}
        {followups.map((followup) => (
          <tr key={followup.id}>
            <td>{followup.lead_name || "-"}</td>
            <td>{fmtDate(followup.scheduled_at)}</td>
            <td>{shortId(followup.zoho_task_id)}</td>
            <td><Badge value={followup.status} /></td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function SyncLogsTable({ logs }: { logs: CrmSyncLog[] }) {
  return (
    <Table title="CRM Sync Logs" count={logs.length}>
      <thead>
        <tr>
          <th>Lead</th>
          <th>Operation</th>
          <th>Result</th>
          <th>Error</th>
          <th>Synced</th>
        </tr>
      </thead>
      <tbody>
        {logs.length === 0 && <EmptyRow colSpan={5} />}
        {logs.map((log) => (
          <tr key={log.id}>
            <td>{log.lead_name || "-"}</td>
            <td>{log.operation}</td>
            <td><Badge value={log.success ? "success" : "failed"} /></td>
            <td className="summaryCell">{log.error_message || "-"}</td>
            <td>{fmtDate(log.synced_at)}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function Table({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="content">
      <div className="tableHeader">
        <h3>{title}</h3>
        <span>{count} records</span>
      </div>
      <div className="tableWrap">
        <table>{children}</table>
      </div>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
