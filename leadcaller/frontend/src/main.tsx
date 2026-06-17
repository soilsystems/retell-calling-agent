import React from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  BarChart3,
  Bot,
  CalendarClock,
  CheckCircle2,
  Database,
  ExternalLink,
  Headphones,
  MessageCircle,
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
  Webhook,
  Paperclip,
  MapPin,
  FileText,
  Download
} from "lucide-react";
import { RetellWebClient } from "retell-client-js-sdk";
import "./styles.css";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const api = (path: string) => `${API_BASE}${path}`;

// ngrok's free tier shows an HTML "browser warning" page on first visit that
// breaks XHR/fetch responses. Sending this header bypasses it for all requests
// going through any ngrok tunnel.
if (API_BASE.includes("ngrok")) {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const headers = new Headers(init.headers || {});
    headers.set("ngrok-skip-browser-warning", "true");
    return originalFetch(input, { ...init, headers });
  };
}

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
  latest_summary?: string | null;
  latest_callback_required?: boolean | null;
  latest_callback_time?: string | null;
  latest_follow_up_required?: boolean | null;
  latest_follow_up_time?: string | null;
};

type CallJob = {
  id: string;
  lead_id: string;
  lead_name?: string | null;
  phone?: string | null;
  status: string;
  trigger_reason?: string | null;
  scheduled_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  retry_count: number;
  max_retries: number;
  created_at?: string | null;
};

type CallAttempt = {
  id: string;
  lead_id?: string | null;
  call_job_id: string;
  lead_name?: string | null;
  phone?: string | null;
  retell_call_id: string;
  attempt_number: number;
  status: string;
  direction?: string | null;
  recording_url?: string | null;
  summary?: string | null;
  transcript?: string | null;
  structured_data?: Record<string, unknown>;
  interest_level?: string | null;
  follow_up_required?: boolean | null;
  follow_up_time?: string | null;
  callback_required?: boolean | null;
  callback_time?: string | null;
  call_outcome?: string | null;
  caller_requirement?: string | null;
  duration_seconds?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
};

type WebhookEvent = {
  id: string;
  source: string;
  event_type: string;
  processed: boolean;
  idempotency_key: string;
  received_at?: string | null;
  payload?: any;
};

type Followup = {
  id: string;
  lead_id: string;
  lead_name?: string | null;
  scheduled_at?: string | null;
  zoho_task_id?: string | null;
  status: string;
};

type CrmSyncLog = {
  id: string;
  lead_id?: string | null;
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

type TabKey = "overview" | "activity" | "leads" | "jobs" | "attempts" | "webhooks" | "followups" | "sync" | "whatsapp";

const tabs: Array<{ key: TabKey; label: string; icon: IconComponent }> = [
  { key: "overview", label: "Overview", icon: BarChart3 },
  { key: "activity", label: "Lead Activity", icon: PhoneCall },
  { key: "leads", label: "Leads", icon: Users },
  { key: "jobs", label: "Call Jobs", icon: Phone },
  { key: "attempts", label: "Attempts", icon: Headphones },
  { key: "webhooks", label: "Webhooks", icon: Webhook },
  { key: "followups", label: "Follow-ups", icon: CalendarClock },
  { key: "sync", label: "CRM Sync", icon: RefreshCcw },
  { key: "whatsapp", label: "WhatsApp", icon: MessageCircle }
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

const isDueSoon = (value?: string | null) => {
  if (!value) return false;
  const time = new Date(value).getTime();
  const now = Date.now();
  return time >= now && time - now <= 60 * 60 * 1000;
};

const byLatestDate = <T extends { started_at?: string | null; scheduled_at?: string | null; created_at?: string | null }>(
  rows: T[]
) => [...rows].sort((a, b) => {
  const left = new Date(a.started_at || a.scheduled_at || a.created_at || 0).getTime();
  const right = new Date(b.started_at || b.scheduled_at || b.created_at || 0).getTime();
  return right - left;
});

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
      const syncRes = await fetch(api("/admin/zoho/sync"), { method: "POST" });
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
        fetch(api("/health")),
        fetch(api("/admin/summary")),
        fetch(api("/admin/leads")),
        fetch(api("/admin/call-jobs")),
        fetch(api("/admin/call-attempts")),
        fetch(api("/admin/webhook-events")),
        fetch(api("/admin/followups")),
        fetch(api("/admin/crm-sync-logs"))
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
  const [activeMode, setActiveMode] = React.useState<"ai" | "human" | "exotel" | null>(null);
  const [showPhoneInput, setShowPhoneInput] = React.useState(false);
  const [agentPhone, setAgentPhone] = React.useState(() => {
    return localStorage.getItem("agent_phone") || "";
  });

  const initiateHuman = async (phone: string) => {
    if (!phone || phone.trim() === "") {
      setError("Please enter your phone number first.");
      return;
    }
    localStorage.setItem("agent_phone", phone.trim());
    setActiveMode("human");
    setCallState("connecting");
    setError(null);
    try {
      const res = await fetch(api(`/admin/leads/${lead.id}/call`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "human", agent_phone: phone.trim() })
      });
      if (!res.ok) throw new Error(await res.text());
      setCallState("ended");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to connect bridge call");
      setCallState("choosing");
    }
  };

  const initiateExotel = async () => {
    setActiveMode("exotel");
    setCallState("connecting");
    setError(null);
    try {
      const res = await fetch(api(`/admin/leads/${lead.id}/call`), {
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
            {!showPhoneInput ? (
              <>
                <button className="callOption aiOption" id="btn-ai-talk" onClick={() => void initiateExotel()}>
                  <div className="callOptionIcon"><Bot size={28} /></div>
                  <div>
                    <strong>Let AI Talk</strong>
                    <span>Exotel places the call and bridges to Retell AI agent automatically</span>
                  </div>
                </button>
                <button className="callOption humanOption" id="btn-human-talk" onClick={() => setShowPhoneInput(true)}>
                  <div className="callOptionIcon"><Mic size={28} /></div>
                  <div>
                    <strong>Let Me Talk</strong>
                    <span>Connect you directly to the lead via physical phone bridge</span>
                  </div>
                </button>
              </>
            ) : (
              <div className="phoneBridgeInput" style={{ width: "100%", padding: "10px 0" }}>
                <p className="callHint" style={{ marginBottom: 12, fontSize: "0.95rem" }}>
                  Enter your phone number to receive the call from Exotel:
                </p>
                <div style={{ display: "flex", gap: 10, width: "100%", justifyContent: "center", marginBottom: 12 }}>
                  <input 
                    type="text" 
                    className="bridgeInput"
                    placeholder="e.g., +91XXXXXXXXXX" 
                    value={agentPhone}
                    onChange={(e) => setAgentPhone(e.target.value)}
                    style={{ 
                      padding: "10px 14px", 
                      borderRadius: 8, 
                      border: "1px solid rgba(255,255,255,0.1)", 
                      background: "rgba(0,0,0,0.2)", 
                      color: "white", 
                      outline: "none",
                      flex: 1,
                      fontSize: "0.95rem"
                    }}
                  />
                  <button 
                    className="bridgeSubmit"
                    onClick={() => void initiateHuman(agentPhone)}
                    style={{
                      padding: "10px 20px",
                      borderRadius: 8,
                      border: "none",
                      background: "linear-gradient(135deg, #2563eb, #1d4ed8)",
                      color: "white",
                      fontWeight: 600,
                      cursor: "pointer",
                      fontSize: "0.95rem"
                    }}
                  >
                    Connect Call
                  </button>
                </div>
                <div style={{ textAlign: "center" }}>
                  <button 
                    className="bridgeCancel"
                    onClick={() => setShowPhoneInput(false)}
                    style={{
                      padding: "6px 14px",
                      borderRadius: 6,
                      border: "1px solid rgba(255,255,255,0.1)",
                      background: "transparent",
                      color: "rgba(255,255,255,0.7)",
                      cursor: "pointer",
                      fontSize: "0.85rem"
                    }}
                  >
                    ← Back to options
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {callState === "connecting" && (
          <div className="callStatus">
            <div className="pulseRing" />
            <p>
              {activeMode === "exotel"
                ? "Requesting Exotel call..."
                : "Requesting Exotel bridge call..."}
            </p>
          </div>
        )}

        {callState === "ended" && (
          <div className="callStatus">
            <CheckCircle2 size={40} color="#047857" />
            <p>
              {activeMode === "exotel"
                ? "Exotel call request sent"
                : activeMode === "human"
                  ? "Bridge call requested. Check your phone!"
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
  const [activeTab, setActiveTab] = React.useState<TabKey>("activity");
  const [query, setQuery] = React.useState("");
  const [callingLead, setCallingLead] = React.useState<Lead | null>(null);
  const [whatsAppTargetPhone, setWhatsAppTargetPhone] = React.useState<string | null>(null);
  const data = useDashboardData();

  const openWhatsAppChat = React.useCallback((lead: Lead) => {
    setWhatsAppTargetPhone(lead.phone);
    setActiveTab("whatsapp");
  }, []);

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

        {activeTab === "overview" && (
          <Overview
            summary={data.summary}
            loading={data.loading}
            leads={data.leads}
            jobs={data.jobs}
            attempts={data.attempts}
            followups={data.followups}
            onOpenActivity={() => setActiveTab("activity")}
          />
        )}
        {activeTab === "activity" && (
          <LeadActivityDashboard
            leads={filteredLeads}
            jobs={data.jobs}
            attempts={data.attempts}
            followups={data.followups}
            syncLogs={data.syncLogs}
            onCallLead={setCallingLead}
            onWhatsAppLead={openWhatsAppChat}
          />
        )}
        {activeTab === "leads" && <LeadsTable leads={filteredLeads} onCallLead={setCallingLead} onWhatsAppLead={openWhatsAppChat} />}
        {activeTab === "jobs" && <JobsTable jobs={data.jobs} onRefresh={data.load} />}
        {activeTab === "attempts" && <AttemptsTable attempts={data.attempts} />}
        {activeTab === "webhooks" && <WebhooksTable webhooks={data.webhooks} />}
        {activeTab === "followups" && <FollowupsTable followups={data.followups} />}
        {activeTab === "sync" && <SyncLogsTable logs={data.syncLogs} />}
        {activeTab === "whatsapp" && <WhatsAppChat initialPhone={whatsAppTargetPhone} leads={data.leads} />}
      </main>
    </div>
  );
}

function Overview({
  summary,
  loading,
  leads,
  jobs,
  attempts,
  followups,
  onOpenActivity
}: {
  summary: Summary | null;
  loading: boolean;
  leads: Lead[];
  jobs: CallJob[];
  attempts: CallAttempt[];
  followups: Followup[];
  onOpenActivity: () => void;
}) {
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
  const pendingCallbacks = jobs
    .filter((job) => job.status === "pending" && job.trigger_reason === "callback_requested")
    .slice(0, 5);
  const latestAttempts = byLatestDate(attempts).slice(0, 5);

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
          <div className="panelTitleRow">
            <h3>Callback Watch</h3>
            <button className="textButton" onClick={onOpenActivity}>
              <span>Open activity</span>
              <ArrowRight size={14} />
            </button>
          </div>
          <div className="pipeline">
            <PipelineItem label="Pending" value={summary?.pending_jobs ?? 0} tone="wait" />
            <PipelineItem label="In Progress" value={summary?.in_progress_jobs ?? 0} tone="info" />
            <PipelineItem label="Completed" value={summary?.completed_jobs ?? 0} tone="good" />
            <PipelineItem label="Failed" value={summary?.failed_jobs ?? 0} tone="bad" />
          </div>
          <div className="miniList">
            {pendingCallbacks.length === 0 ? (
              <div className="emptyCard">No pending callback jobs right now</div>
            ) : pendingCallbacks.map((job) => (
              <div className={isDueSoon(job.scheduled_at) ? "miniRow urgent" : "miniRow"} key={job.id}>
                <div>
                  <strong>{job.lead_name || "Unknown lead"}</strong>
                  <span>{job.phone || "-"}</span>
                </div>
                <div className="miniMeta">
                  <Badge value={job.status} />
                  <span>{fmtDate(job.scheduled_at)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <h3>Latest Call Notes</h3>
          <div className="miniList">
            {latestAttempts.length === 0 ? (
              <div className="emptyCard">No call summaries yet</div>
            ) : latestAttempts.map((attempt) => (
              <div className="notePreview" key={attempt.id}>
                <div className="notePreviewHead">
                  <strong>{attempt.lead_name || "Unknown lead"}</strong>
                  <Badge value={attempt.call_outcome || attempt.status} />
                </div>
                <p>{attempt.summary || attempt.caller_requirement || "No summary captured yet."}</p>
                <span>{fmtDate(attempt.started_at)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function LeadActivityDashboard({
  leads,
  jobs,
  attempts,
  followups,
  syncLogs,
  onCallLead,
  onWhatsAppLead
}: {
  leads: Lead[];
  jobs: CallJob[];
  attempts: CallAttempt[];
  followups: Followup[];
  syncLogs: CrmSyncLog[];
  onCallLead: (lead: Lead) => void;
  onWhatsAppLead: (lead: Lead) => void;
}) {
  const pendingCallbacks = jobs.filter((job) => job.status === "pending" && job.trigger_reason === "callback_requested");
  const dueSoon = pendingCallbacks.filter((job) => isDueSoon(job.scheduled_at));
  const recentFailures = syncLogs.filter((log) => !log.success).slice(0, 5);
  const rows = leads.map((lead) => {
    const leadJobs = jobs.filter((job) => job.lead_id === lead.id || job.phone === lead.phone);
    const leadAttempts = byLatestDate(attempts.filter((attempt) => attempt.lead_id === lead.id || attempt.phone === lead.phone));
    const leadFollowups = followups.filter((followup) => followup.lead_id === lead.id);
    const latestAttempt = leadAttempts[0];
    const nextCallback = leadJobs
      .filter((job) => job.status === "pending" && job.trigger_reason === "callback_requested")
      .sort((a, b) => new Date(a.scheduled_at || 0).getTime() - new Date(b.scheduled_at || 0).getTime())[0];
    return { lead, leadJobs, leadAttempts, leadFollowups, latestAttempt, nextCallback };
  });

  return (
    <section className="content">
      <div className="opsGrid">
        <div className="opsStat">
          <span>Pending callbacks</span>
          <strong>{pendingCallbacks.length}</strong>
        </div>
        <div className="opsStat urgentStat">
          <span>Due within 1 hour</span>
          <strong>{dueSoon.length}</strong>
        </div>
        <div className="opsStat">
          <span>Calls with notes</span>
          <strong>{attempts.filter((attempt) => attempt.summary || attempt.caller_requirement).length}</strong>
        </div>
        <div className="opsStat">
          <span>Sync failures</span>
          <strong>{recentFailures.length}</strong>
        </div>
      </div>

      <div className="activityList">
        {rows.length === 0 && <div className="emptyCard">No matching leads</div>}
        {rows.map(({ lead, leadAttempts, leadFollowups, latestAttempt, nextCallback }) => (
          <article className="leadCard" key={lead.id}>
            <div className="leadCardTop">
              <div>
                <h3>{lead.name}</h3>
                <p>{lead.phone} {lead.city ? `• ${lead.city}` : ""}</p>
              </div>
              <div className="leadActions">
                <button className="callLeadBtn" onClick={() => onCallLead(lead)} title={`Call ${lead.name}`}>
                  <Phone size={13} />
                  <span>Call</span>
                </button>
                <button className="callLeadBtn whatsappAction" onClick={() => onWhatsAppLead(lead)} title={`WhatsApp ${lead.name}`}>
                  <MessageCircle size={13} />
                  <span>Chat</span>
                </button>
              </div>
            </div>

            <div className="leadInsightGrid">
              <div className={nextCallback ? "insightBox callbackBox" : "insightBox"}>
                <span>Next callback</span>
                <strong>{nextCallback ? fmtDate(nextCallback.scheduled_at) : "None scheduled"}</strong>
                <small>{nextCallback?.trigger_reason || lead.latest_callback_time || "-"}</small>
              </div>
              <div className="insightBox">
                <span>Latest outcome</span>
                <strong>{latestAttempt?.call_outcome || latestAttempt?.status || lead.latest_attempt_status || "-"}</strong>
                <small>{latestAttempt?.direction || lead.latest_interest_level || "-"}</small>
              </div>
              <div className="insightBox">
                <span>Intent</span>
                <strong>{latestAttempt?.interest_level || lead.latest_interest_level || "-"}</strong>
                <small>{lead.campaign || lead.source || "-"}</small>
              </div>
              <div className="insightBox">
                <span>Follow-up task</span>
                <strong>{leadFollowups[0] ? fmtDate(leadFollowups[0].scheduled_at) : "None"}</strong>
                <small>{leadFollowups[0]?.status || "-"}</small>
              </div>
            </div>

            <div className="notesBlock">
              <span>Latest notes / summary</span>
              <p>{latestAttempt?.summary || latestAttempt?.caller_requirement || lead.latest_summary || "No notes captured yet."}</p>
            </div>

            <div className="callTimeline">
              {leadAttempts.length === 0 ? (
                <div className="emptyCard compact">No call history yet</div>
              ) : leadAttempts.slice(0, 4).map((attempt) => (
                <div className="timelineItem" key={attempt.id}>
                  <div className="timelineDot" />
                  <div>
                    <div className="timelineHead">
                      <strong>{fmtDate(attempt.started_at)}</strong>
                      <Badge value={attempt.call_outcome || attempt.status} />
                    </div>
                    <p>{attempt.summary || attempt.caller_requirement || "No summary captured."}</p>
                    <div className="timelineMeta">
                      <span>{attempt.direction || "-"} call</span>
                      <span>{attempt.duration_seconds ? `${attempt.duration_seconds}s` : "duration -"}</span>
                      {attempt.callback_required && <span>callback: {String(attempt.callback_time || "requested")}</span>}
                      {attempt.follow_up_required && <span>follow-up: {String(attempt.follow_up_time || "requested")}</span>}
                      {attempt.recording_url && (
                        <a href={attempt.recording_url} target="_blank" rel="noreferrer">
                          Recording <ExternalLink size={12} />
                        </a>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </article>
        ))}
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

function LeadsTable({
  leads,
  onCallLead,
  onWhatsAppLead
}: {
  leads: Lead[];
  onCallLead: (lead: Lead) => void;
  onWhatsAppLead: (lead: Lead) => void;
}) {
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
          <th>Callback / Notes</th>
          <th>Created</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {leads.length === 0 && <EmptyRow colSpan={9} />}
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
            <td className="summaryCell">
              {lead.latest_callback_required ? `Callback: ${lead.latest_callback_time || "requested"}` : lead.latest_summary || "-"}
            </td>
            <td>{fmtDate(lead.created_at)}</td>
            <td>
              <div style={{ display: "flex", gap: 6 }}>
                <button
                  id={`call-lead-${lead.id}`}
                  className="callLeadBtn"
                  onClick={() => onCallLead(lead)}
                  title={`Call ${lead.name}`}
                >
                  <Phone size={13} />
                  <span>Call</span>
                </button>
                <button
                  id={`wa-lead-${lead.id}`}
                  className="callLeadBtn"
                  style={{ backgroundColor: "#25d366", borderColor: "#25d366" }}
                  onClick={() => onWhatsAppLead(lead)}
                  title={`WhatsApp ${lead.name}`}
                >
                  <MessageCircle size={13} />
                  <span>Chat</span>
                </button>
              </div>
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
      const response = await fetch(api(`/admin/call-jobs/${id}/trigger`), { method: "POST" });
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
          <th>Reason</th>
          <th>Scheduled</th>
          <th>Retries</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {jobs.length === 0 && <EmptyRow colSpan={7} />}
        {jobs.map((job) => (
          <tr key={job.id}>
            <td>
              <strong>{job.lead_name || "-"}</strong>
              <small>{shortId(job.id)}</small>
            </td>
            <td>{job.phone || "-"}</td>
            <td><Badge value={job.status} /></td>
            <td>{job.trigger_reason || "-"}</td>
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
          <th>Callback</th>
          <th>Duration</th>
          <th>Summary</th>
          <th>Recording</th>
        </tr>
      </thead>
      <tbody>
        {attempts.length === 0 && <EmptyRow colSpan={7} />}
        {attempts.map((attempt) => (
          <tr key={attempt.id}>
            <td>
              <strong>{attempt.lead_name || "-"}</strong>
              <small>{attempt.phone || shortId(attempt.retell_call_id)}</small>
            </td>
            <td><Badge value={attempt.status} /></td>
            <td><Badge value={attempt.interest_level} /></td>
            <td>{attempt.callback_required ? String(attempt.callback_time || "requested") : "-"}</td>
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
  const [expandedId, setExpandedId] = React.useState<string | null>(null);

  return (
    <Table title="Webhook Events" count={webhooks.length}>
      <thead>
        <tr>
          <th>Source</th>
          <th>Type</th>
          <th>Processed</th>
          <th>Idempotency</th>
          <th>Received</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {webhooks.length === 0 && <EmptyRow colSpan={6} />}
        {webhooks.map((event) => {
          const isExpanded = expandedId === event.id;
          return (
            <React.Fragment key={event.id}>
              <tr onClick={() => setExpandedId(isExpanded ? null : event.id)} style={{ cursor: "pointer" }}>
                <td><Badge value={event.source} /></td>
                <td>{event.event_type}</td>
                <td><Badge value={event.processed ? "processed" : "pending"} /></td>
                <td>{shortId(event.idempotency_key)}</td>
                <td>{fmtDate(event.received_at)}</td>
                <td>
                  <button className="textButton" style={{ padding: "4px 8px", fontSize: "12px", border: "1px solid rgba(255,255,255,0.1)", borderRadius: "4px", background: "rgba(255,255,255,0.02)" }}>
                    {isExpanded ? "Hide Details" : "View Details"}
                  </button>
                </td>
              </tr>
              {isExpanded && (
                <tr>
                  <td colSpan={6} style={{ backgroundColor: "rgba(0, 0, 0, 0.15)", padding: "12px 20px" }}>
                    <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                      <span style={{ fontSize: "13px", fontWeight: "bold", color: "#94a3b8" }}>Event Payload:</span>
                      <pre style={{
                        margin: 0,
                        padding: "12px",
                        background: "rgba(0, 0, 0, 0.3)",
                        border: "1px solid rgba(255, 255, 255, 0.05)",
                        borderRadius: "6px",
                        overflowX: "auto",
                        fontFamily: "monospace",
                        fontSize: "12px",
                        color: "#e2e8f0",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-all",
                        lineHeight: "1.4"
                      }}>
                        {JSON.stringify(event.payload, null, 2)}
                      </pre>
                    </div>
                  </td>
                </tr>
              )}
            </React.Fragment>
          );
        })}
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

// ─── WhatsApp Modal (legacy template-sender, kept for reference) ───────────
// eslint-disable-next-line @typescript-eslint/no-unused-vars
function _WhatsAppModal({ lead, onClose }: { lead: Lead; onClose: () => void }) {
  const [loading, setLoading] = React.useState(false);
  const [successMsg, setSuccessMsg] = React.useState<string | null>(null);
  const [errorMsg, setErrorMsg] = React.useState<string | null>(null);
  const [customText, setCustomText] = React.useState("");

  const triggerNudge = async (type: "completed" | "missed") => {
    setLoading(true);
    setErrorMsg(null);
    setSuccessMsg(null);
    try {
      const res = await fetch(api("/whatsapp/send-nudge"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: lead.id, nudge_type: type })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to trigger WhatsApp nudge");
      setSuccessMsg(data.message || `Successfully sent ${type} message!`);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Error sending message");
    } finally {
      setLoading(false);
    }
  };

  const sendCustom = async () => {
    if (!customText.trim()) return;
    setLoading(true);
    setErrorMsg(null);
    setSuccessMsg(null);
    try {
      const res = await fetch(api("/whatsapp/send-custom"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: lead.phone, text: customText })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to send custom message");
      setSuccessMsg("Custom WhatsApp message sent!");
      setCustomText("");
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Error sending message");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modalOverlay" onClick={onClose}>
      <div className="modalCard" onClick={(e) => e.stopPropagation()}>
        <div className="modalHeader">
          <div className="modalLeadInfo">
            <div className="modalAvatar" style={{ backgroundColor: "#25d366" }}><MessageCircle size={20} /></div>
            <div>
              <strong>WhatsApp Follow-up</strong>
              <span>{lead.name} ({lead.phone})</span>
            </div>
          </div>
          <button className="modalClose" onClick={onClose} title="Close">✕</button>
        </div>

        {successMsg && (
          <div className="notice goodNotice" style={{ margin: "16px 20px" }}>
            <CheckCircle2 size={16} />
            <span>{successMsg}</span>
          </div>
        )}

        {errorMsg && (
          <div className="notice badNotice" style={{ margin: "16px 20px" }}>
            <AlertCircle size={16} />
            <span>{errorMsg}</span>
          </div>
        )}

        <div className="callOptions" style={{ padding: "0 20px 20px" }}>
          <p className="callHint">Send a pre-approved template or custom message</p>
          
          <button 
            className="callOption" 
            style={{ borderColor: "rgba(37, 211, 102, 0.2)", width: "100%", cursor: "pointer" }} 
            disabled={loading}
            onClick={() => void triggerNudge("completed")}
          >
            <div className="callOptionIcon" style={{ color: "#25d366" }}><CheckCircle2 size={24} /></div>
            <div style={{ textAlign: "left" }}>
              <strong>Send Completed Call Follow-up</strong>
              <span>Pre-approved Exotel template with booking link</span>
            </div>
          </button>

          <button 
            className="callOption" 
            style={{ borderColor: "rgba(37, 211, 102, 0.2)", width: "100%", cursor: "pointer" }}
            disabled={loading}
            onClick={() => void triggerNudge("missed")}
          >
            <div className="callOptionIcon" style={{ color: "#eab308" }}><PhoneOff size={24} /></div>
            <div style={{ textAlign: "left" }}>
              <strong>Send Missed Call Nudge</strong>
              <span>Pre-approved Exotel template for no-answers</span>
            </div>
          </button>

          <div style={{ marginTop: 20 }}>
            <label style={{ display: "block", marginBottom: 6, fontWeight: "bold", fontSize: 13, color: "#94a3b8" }}>
              Custom Direct Message
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="customInput"
                style={{
                  flex: 1,
                  background: "rgba(255, 255, 255, 0.05)",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  borderRadius: 6,
                  padding: "8px 12px",
                  color: "#fff",
                  outline: "none"
                }}
                placeholder="Type custom text..."
                value={customText}
                onChange={(e) => setCustomText(e.target.value)}
                disabled={loading}
              />
              <button 
                className="callLeadBtn" 
                style={{ backgroundColor: "#25d366", borderColor: "#25d366", color: "#fff" }}
                disabled={loading || !customText.trim()}
                onClick={() => void sendCustom()}
              >
                <Send size={14} />
                <span>Send</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── WhatsApp Panel (legacy template-sender, kept for reference) ──────────
// eslint-disable-next-line @typescript-eslint/no-unused-vars
function _WhatsAppPanel({ leads, onRefresh }: { leads: Lead[]; onRefresh: () => void }) {
  const [phone, setPhone] = React.useState("");
  const [name, setName] = React.useState("");
  const [template, setTemplate] = React.useState<"completed" | "missed">("completed");
  const [activeLead, setActiveLead] = React.useState<string>("");
  const [loading, setLoading] = React.useState(false);
  const [success, setSuccess] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const handleSendTemplate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!phone || !name) return;
    setLoading(true);
    setError(null);
    setSuccess(null);
    try {
      const res = await fetch(api("/whatsapp/send-template"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, lead_name: name, template_type: template })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to send template");
      setSuccess(`WhatsApp template message successfully enqueued for ${name}!`);
      setPhone("");
      setName("");
      setActiveLead("");
      void onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error sending message");
    } finally {
      setLoading(false);
    }
  };

  React.useEffect(() => {
    if (!activeLead) return;
    const lead = leads.find(l => l.id === activeLead);
    if (lead) {
      setPhone(lead.phone);
      setName(lead.name);
    }
  }, [activeLead, leads]);

  React.useEffect(() => {
    if (!success && !error) return;
    const id = window.setTimeout(() => {
      setSuccess(null);
      setError(null);
    }, 4500);
    return () => window.clearTimeout(id);
  }, [success, error]);

  const sortedLeads = React.useMemo(
    () => [...leads].sort((a, b) => (a.name || "").localeCompare(b.name || "")),
    [leads]
  );

  const templateLabels = {
    completed: "Completed Follow-up",
    missed: "Missed Call Nudge"
  } as const;

  return (
    <section className="content">
      {success && (
        <div className="notice goodNotice">
          <CheckCircle2 size={18} />
          <span>{success}</span>
          <button
            type="button"
            onClick={() => setSuccess(null)}
            style={{ marginLeft: "auto", background: "none", border: "none", color: "inherit", cursor: "pointer", fontSize: 18, lineHeight: 1, padding: "0 4px" }}
            aria-label="Dismiss"
          >×</button>
        </div>
      )}
      {error && (
        <div className="notice badNotice">
          <AlertCircle size={18} />
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            style={{ marginLeft: "auto", background: "none", border: "none", color: "inherit", cursor: "pointer", fontSize: 18, lineHeight: 1, padding: "0 4px" }}
            aria-label="Dismiss"
          >×</button>
        </div>
      )}

      <div className="split">
        <div className="panel">
          <h3>Exotel WhatsApp Configuration</h3>
          <div className="pipeline" style={{ gap: 12, marginTop: 16 }}>
            <div className="pipelineItem info" style={{ padding: 16, borderRadius: 8 }}>
              <span>Integration Status</span>
              <strong>Active & Configured</strong>
            </div>
            <div className="pipelineItem good" style={{ padding: 16, borderRadius: 8 }}>
              <span>Provider</span>
              <strong>Exotel v2 API</strong>
            </div>
          </div>
          <div className="links" style={{ marginTop: 24 }}>
            <a href="https://my.exotel.com" target="_blank" rel="noreferrer">Exotel Dashboard <ExternalLink size={14} /></a>
            <a href="https://business.facebook.com" target="_blank" rel="noreferrer">Meta WhatsApp Manager <ExternalLink size={14} /></a>
          </div>
        </div>

        <div className="panel">
          <h3>Trigger Template Message</h3>
          <div style={{ display: "flex", gap: 12, marginBottom: 20 }}>
            <button 
              type="button"
              className="tableAction" 
              style={{ flex: 1, backgroundColor: template === "completed" ? "#25d366" : "#f1f5f9", border: "1px solid #e2e8f0", color: template === "completed" ? "#fff" : "#475569", cursor: "pointer", fontWeight: "bold", borderRadius: 6, padding: "10px" }}
              onClick={() => setTemplate("completed")}
            >
              {templateLabels.completed}
            </button>
            <button
              type="button"
              className="tableAction"
              style={{ flex: 1, backgroundColor: template === "missed" ? "#eab308" : "#f1f5f9", border: "1px solid #e2e8f0", color: template === "missed" ? "#fff" : "#475569", cursor: "pointer", fontWeight: "bold", borderRadius: 6, padding: "10px" }}
              onClick={() => setTemplate("missed")}
            >
              {templateLabels.missed}
            </button>
          </div>

          <form onSubmit={handleSendTemplate} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div>
              <label style={{ display: "block", fontSize: 12, marginBottom: 6, color: "#64748b" }}>Select Lead (Optional)</label>
              <select
                value={activeLead}
                onChange={e => setActiveLead(e.target.value)}
                style={{ width: "100%", padding: "10px", background: "#fff", border: "1px solid #e2e8f0", borderRadius: 6, color: "#1e293b", outline: "none" }}
              >
                <option value="">-- Choose Lead --</option>
                {sortedLeads.map(l => (
                  <option key={l.id} value={l.id}>{l.name} ({l.phone})</option>
                ))}
              </select>
            </div>
            <div>
              <label style={{ display: "block", fontSize: 12, marginBottom: 6, color: "#64748b" }}>Lead Name</label>
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="e.g. Darshan"
                required
                style={{ width: "100%", padding: "10px", background: "#fff", border: "1px solid #e2e8f0", borderRadius: 6, color: "#1e293b", outline: "none" }}
              />
            </div>
            <div>
              <label style={{ display: "block", fontSize: 12, marginBottom: 6, color: "#64748b" }}>Phone Number (with Country Code)</label>
              <input
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder="e.g. +91XXXXXXXXXX"
                required
                style={{ width: "100%", padding: "10px", background: "#fff", border: "1px solid #e2e8f0", borderRadius: 6, color: "#1e293b", outline: "none" }}
              />
            </div>
            <button 
              type="submit" 
              disabled={loading} 
              className="callLeadBtn" 
              style={{ backgroundColor: "#25d366", borderColor: "#25d366", color: "#fff", width: "100%", padding: "12px", marginTop: 10, cursor: "pointer" }}
            >
              <Send size={15} />
              <span>{loading ? "Sending Notification..." : `Send ${templateLabels[template]}`}</span>
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}

// ─── WhatsApp Chat (two-pane two-way conversation UI) ─────────────────────

type ChatMessage = {
  id: string;
  phone: string;
  direction: "inbound" | "outbound";
  type: "text" | "image" | "document" | "video" | "audio" | "location" | "template" | "other";
  body: string | null;
  media_url: string | null;
  media_filename: string | null;
  caption: string | null;
  latitude: string | null;
  longitude: string | null;
  location_name: string | null;
  created_at: string | null;
};

type ChatConversation = {
  phone: string;
  lead_name: string | null;
  last_message: ChatMessage;
  last_at: string | null;
};

type ChatThread = {
  phone: string;
  lead_name: string | null;
  lead_id: string | null;
  messages: ChatMessage[];
};

function shortPreview(m: ChatMessage): string {
  if (m.type === "text") return m.body || "";
  if (m.type === "location") return "📍 Location";
  if (m.type === "image") return "📷 Image" + (m.caption ? `: ${m.caption}` : "");
  if (m.type === "video") return "🎬 Video" + (m.caption ? `: ${m.caption}` : "");
  if (m.type === "document") return "📄 " + (m.media_filename || "Document");
  if (m.type === "audio") return "🎵 Audio";
  return m.body || `[${m.type}]`;
}

function WhatsAppChat({ initialPhone, leads }: { initialPhone: string | null; leads: Lead[] }) {
  const [conversations, setConversations] = React.useState<ChatConversation[]>([]);
  const [selectedPhone, setSelectedPhone] = React.useState<string | null>(initialPhone);
  const [thread, setThread] = React.useState<ChatThread | null>(null);
  const [composeText, setComposeText] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const messagesEndRef = React.useRef<HTMLDivElement | null>(null);

  // Build a merged list: backend conversations + leads with no conversation yet
  const mergedList = React.useMemo<ChatConversation[]>(() => {
    const byPhone = new Map<string, ChatConversation>();
    for (const c of conversations) byPhone.set(c.phone, c);
    for (const lead of leads) {
      if (!byPhone.has(lead.phone)) {
        byPhone.set(lead.phone, {
          phone: lead.phone,
          lead_name: lead.name,
          last_message: {
            id: `placeholder-${lead.id}`,
            phone: lead.phone,
            direction: "outbound",
            type: "text",
            body: "(no messages yet)",
            media_url: null,
            media_filename: null,
            caption: null,
            latitude: null,
            longitude: null,
            location_name: null,
            created_at: null,
          },
          last_at: null,
        });
      } else if (!byPhone.get(lead.phone)!.lead_name) {
        byPhone.get(lead.phone)!.lead_name = lead.name;
      }
    }
    return Array.from(byPhone.values()).sort((a, b) => {
      const ta = a.last_at ? new Date(a.last_at).getTime() : 0;
      const tb = b.last_at ? new Date(b.last_at).getTime() : 0;
      return tb - ta;
    });
  }, [conversations, leads]);

  React.useEffect(() => {
    if (initialPhone) setSelectedPhone(initialPhone);
  }, [initialPhone]);

  const loadConversations = React.useCallback(async () => {
    try {
      const r = await fetch("/whatsapp/conversations");
      if (!r.ok) throw new Error(`/whatsapp/conversations -> ${r.status}`);
      const data: ChatConversation[] = await r.json();
      setConversations(data);
    } catch (e) {
      console.warn("loadConversations failed:", e);
    }
  }, []);

  const loadThread = React.useCallback(async (phone: string) => {
    try {
      const r = await fetch(`/whatsapp/conversations/${encodeURIComponent(phone)}`);
      if (!r.ok) throw new Error(`/whatsapp/conversations/${phone} -> ${r.status}`);
      const data: ChatThread = await r.json();
      setThread(data);
    } catch (e) {
      console.warn("loadThread failed:", e);
    }
  }, []);

  React.useEffect(() => {
    void loadConversations();
    const id = window.setInterval(() => {
      void loadConversations();
      if (selectedPhone) void loadThread(selectedPhone);
    }, 5000);
    return () => window.clearInterval(id);
  }, [loadConversations, loadThread, selectedPhone]);

  React.useEffect(() => {
    if (selectedPhone) void loadThread(selectedPhone);
    else setThread(null);
  }, [selectedPhone, loadThread]);

  React.useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread?.messages.length]);

  const sendMessage = async (payload: Record<string, unknown>) => {
    if (!selectedPhone) return;
    setSending(true);
    setError(null);
    try {
      const r = await fetch(`/whatsapp/conversations/${encodeURIComponent(selectedPhone)}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));
      await loadThread(selectedPhone);
      await loadConversations();
    } catch (e) {
      setError(e instanceof Error ? e.message : "send failed");
    } finally {
      setSending(false);
    }
  };

  const sendText = async () => {
    if (!composeText.trim() || !selectedPhone) return;
    const body = composeText;
    setComposeText("");
    await sendMessage({ type: "text", body });
  };

  const handleFile = async (file: File) => {
    if (!selectedPhone) return;
    setUploading(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const upRes = await fetch("/whatsapp/upload", { method: "POST", body: fd });
      const upJson = await upRes.json();
      if (!upRes.ok) throw new Error(upJson.detail || "upload failed");
      const url: string = upJson.url;
      const mime = (file.type || "").toLowerCase();
      let type: "image" | "video" | "audio" | "document" = "document";
      if (mime.startsWith("image/")) type = "image";
      else if (mime.startsWith("video/")) type = "video";
      else if (mime.startsWith("audio/")) type = "audio";
      const payload: Record<string, unknown> = { type, media_url: url };
      if (type === "document") payload.media_filename = file.name;
      await sendMessage(payload);
    } catch (e) {
      setError(e instanceof Error ? e.message : "upload failed");
    } finally {
      setUploading(false);
    }
  };

  const sendLocation = () => {
    if (!selectedPhone) return;
    if (!navigator.geolocation) {
      setError("geolocation not supported by this browser");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        await sendMessage({
          type: "location",
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          location_name: "Current location",
        });
      },
      (err) => setError(`location error: ${err.message}`),
    );
  };

  return (
    <section className="content" style={{ height: "calc(100vh - 180px)", display: "flex" }}>
      <div className="waChatWrap">
        <aside className="waConvList">
          <div className="waConvHeader">
            <strong>Conversations</strong>
            <span>{mergedList.length}</span>
          </div>
          {mergedList.map((c) => (
            <button
              key={c.phone}
              className={"waConvRow" + (c.phone === selectedPhone ? " active" : "")}
              onClick={() => setSelectedPhone(c.phone)}
            >
              <div className="waConvAvatar">{(c.lead_name || c.phone).slice(0, 1).toUpperCase()}</div>
              <div className="waConvBody">
                <div className="waConvTopLine">
                  <strong>{c.lead_name || c.phone}</strong>
                  <span>{c.last_at ? fmtDate(c.last_at) : ""}</span>
                </div>
                <span className="waConvPreview">{shortPreview(c.last_message)}</span>
              </div>
            </button>
          ))}
          {mergedList.length === 0 && <div className="emptyCard">No conversations yet</div>}
        </aside>

        <main className="waThread">
          {!selectedPhone && <div className="waEmpty">Select a conversation to start chatting</div>}
          {selectedPhone && (
            <>
              <header className="waThreadHeader">
                <div className="waConvAvatar">{(thread?.lead_name || selectedPhone).slice(0, 1).toUpperCase()}</div>
                <div>
                  <strong>{thread?.lead_name || selectedPhone}</strong>
                  <span>{selectedPhone}</span>
                </div>
              </header>

              <div className="waMessages">
                {thread?.messages.map((m) => (
                  <MessageBubble key={m.id} m={m} />
                ))}
                <div ref={messagesEndRef} />
                {(!thread || thread.messages.length === 0) && (
                  <div className="emptyCard">No messages yet — say hello!</div>
                )}
              </div>

              {error && (
                <div className="notice badNotice" style={{ margin: "0 14px 8px" }}>
                  <AlertCircle size={14} />
                  <span>{error}</span>
                </div>
              )}

              <div className="waCompose">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*,video/*,audio/*,application/pdf,.doc,.docx,.xls,.xlsx,.txt"
                  style={{
                    position: "absolute",
                    width: 1,
                    height: 1,
                    padding: 0,
                    margin: -1,
                    overflow: "hidden",
                    clip: "rect(0,0,0,0)",
                    border: 0,
                    opacity: 0,
                  }}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) {
                      console.log("[chat] file selected", f.name, f.size, f.type);
                      void handleFile(f);
                    }
                    e.target.value = "";
                  }}
                />
                <button
                  className="waComposeBtn"
                  disabled={uploading || sending}
                  onClick={(ev) => {
                    ev.preventDefault();
                    fileInputRef.current?.click();
                  }}
                  type="button"
                  title="Attach file (image, video, audio, document)"
                >
                  <Paperclip size={18} />
                </button>
                <button
                  className="waComposeBtn"
                  disabled={sending}
                  onClick={sendLocation}
                  title="Send location"
                >
                  <MapPin size={18} />
                </button>
                <input
                  className="waComposeInput"
                  placeholder={uploading ? "Uploading..." : "Type a message"}
                  value={composeText}
                  onChange={(e) => setComposeText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void sendText();
                    }
                  }}
                  disabled={sending || uploading}
                />
                <button
                  className="waSendBtn"
                  disabled={!composeText.trim() || sending || uploading}
                  onClick={() => void sendText()}
                  title="Send"
                >
                  <Send size={18} />
                </button>
              </div>
            </>
          )}
        </main>
      </div>
    </section>
  );
}

function MessageBubble({ m }: { m: ChatMessage }) {
  const cls = "waBubble " + (m.direction === "outbound" ? "out" : "in");
  return (
    <div className={cls}>
      {m.type === "text" && <span>{m.body}</span>}
      {m.type === "image" && m.media_url && (
        <>
          <a href={m.media_url} target="_blank" rel="noreferrer">
            <img src={m.media_url} alt="" style={{ maxWidth: 240, borderRadius: 8 }} />
          </a>
          {m.caption && <span style={{ marginTop: 6 }}>{m.caption}</span>}
        </>
      )}
      {m.type === "video" && m.media_url && (
        <>
          <video src={m.media_url} controls style={{ maxWidth: 240, borderRadius: 8 }} />
          {m.caption && <span style={{ marginTop: 6 }}>{m.caption}</span>}
        </>
      )}
      {m.type === "audio" && m.media_url && <audio src={m.media_url} controls />}
      {m.type === "document" && m.media_url && (
        <a className="waDocLink" href={m.media_url} target="_blank" rel="noreferrer">
          <FileText size={16} />
          <span>{m.media_filename || "Document"}</span>
          <Download size={14} />
        </a>
      )}
      {m.type === "location" && m.latitude && m.longitude && (
        <a
          className="waDocLink"
          href={`https://maps.google.com/?q=${m.latitude},${m.longitude}`}
          target="_blank"
          rel="noreferrer"
        >
          <MapPin size={16} />
          <span>{m.location_name || `${m.latitude},${m.longitude}`}</span>
        </a>
      )}
      <span className="waBubbleTime">{m.created_at ? fmtDate(m.created_at) : ""}</span>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
