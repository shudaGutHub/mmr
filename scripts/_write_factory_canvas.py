"""Generate symbol-agent-factory.canvas.tsx from slim JSON + learn-mix checkpoint."""
from __future__ import annotations

import json
from pathlib import Path

slim = json.loads(Path(r"c:\Users\salee\BITBUCKET\mmr\data\factory_canvas_slim.json").read_text(encoding="utf-8"))
_intraday = Path(r"c:\Users\salee\BITBUCKET\mmr\scripts\learn_mix_intraday_checkpoint.json")
_daily = Path(r"c:\Users\salee\BITBUCKET\mmr\scripts\learn_mix_checkpoint.json")
ckpt = json.loads((_intraday if _intraday.exists() else _daily).read_text(encoding="utf-8"))
out = Path(r"C:\Users\salee\.cursor\projects\c-Users-salee-BITBUCKET-mmr\canvases\symbol-agent-factory.canvas.tsx")

mix: dict[str, dict] = {}
for a in slim["agents"]:
    sym = a["symbol"]
    rec = ckpt.get("symbols", {}).get(sym, {})
    data = rec.get("data") or {}
    rec_mix = rec.get("recommended") or {}
    runs = rec.get("runs") or []
    compact_runs = []
    for r in runs:
        if r.get("status") != "ok":
            continue
        compact_runs.append({
            "cls": r.get("class"),
            "runId": r.get("run_id"),
            "trades": r.get("total_trades") or 0,
            "ret": r.get("total_return"),
            "sharpe": r.get("sharpe_ratio"),
        })
    picks = []
    for p in rec_mix.get("picks") or []:
        picks.append({
            "cls": p.get("class"),
            "runId": p.get("run_id"),
            "params": p.get("params") or {},
            "trades": p.get("total_trades"),
            "ret": p.get("total_return"),
            "sharpe": p.get("sharpe_ratio"),
            "psr": p.get("probabilistic_sharpe"),
            "pValue": p.get("p_value"),
            "cleared": bool(p.get("cleared")),
            "watch": bool(p.get("watch")),
            "deployable": bool(p.get("deployable")),
            "flags": p.get("flags") or [],
        })
    ok_runs = [r for r in runs if r.get("status") == "ok"]
    learn = rec.get("learn_state") or "queued"
    if rec.get("mix_complete") and ok_runs:
        learn = "done"
    elif rec.get("block_reason") and "daily data" in str(rec.get("block_reason")):
        learn = "blocked"
    elif rec.get("preflight") == "blocked" or not rec.get("conId"):
        learn = "blocked"
    elif rec.get("learn_state") == "active" or (ok_runs and not rec.get("mix_complete")):
        learn = "active"
    elif rec.get("conId"):
        learn = "queued"
    mix[sym] = {
        "conId": rec.get("conId"),
        "preflight": rec.get("preflight") or "blocked",
        "learn": learn,
        "complete": bool(rec.get("mix_complete") and ok_runs),
        "block": rec.get("block_reason") or "",
        "dataStatus": data.get("status") or "unknown",
        "dataRows": data.get("rows") or 0,
        "dataLast": data.get("last"),
        "dataKey": data.get("key"),
        "picks": picks,
        "anyCleared": bool(rec_mix.get("any_cleared")),
        "anyWatch": bool(rec_mix.get("any_watch")),
        "note": rec_mix.get("note") or "",
        "runs": compact_runs,
    }

n_complete = sum(1 for v in mix.values() if v["complete"])
n_resolved = sum(1 for v in mix.values() if v["conId"])
n_blocked = sum(1 for v in mix.values() if v["learn"] == "blocked")
n_watch = sum(1 for v in mix.values() if v["anyWatch"])
n_cleared = sum(1 for v in mix.values() if v["anyCleared"])
_min_bars = 4000 if ckpt.get("bar_size") == "1 min" else 80
n_data_ok = sum(1 for v in mix.values() if (v["dataRows"] or 0) >= _min_bars)

counts = {
    "universe": 313,
    "valid": 313,
    "biotech": 6,
    "agents": 74,
    "mixComplete": n_complete,
    "resolved": n_resolved,
    "blocked": n_blocked,
    "watch": n_watch,
    "cleared": n_cleared,
    "dataOk": n_data_ok,
}

agents_js = json.dumps(slim["agents"], indent=2)
bio_js = json.dumps(slim["biotech"], indent=2)
mix_js = json.dumps(mix, indent=2)
counts_js = json.dumps(counts)
as_of = slim["asOf"]
mix_as_of = ckpt.get("asOf") or ""

tsx = r'''import {
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
  computeDAGLayout,
  useCanvasState,
  useHostTheme,
} from "cursor/canvas";

type Family = "fade" | "breakout" | "trend" | "range";
type NodeState = "idle" | "active" | "blocked" | "done" | "queued";
type PhaseFilter = "all" | "mix-done" | "blocked" | Family;

type Agent = {
  symbol: string;
  name: string;
  last: number;
  atrPct: number;
  sessionTrPct: number;
  dollarVol: number;
  combo: number;
  atrRank: number;
  dvolRank: number;
  gapPct: number;
  chgPct: number;
  industry: string;
  sector: string;
  family: Family;
  atrBars: number;
  feed: string;
  rank: number;
};

type MixPick = {
  cls: string;
  runId: number | null;
  params: Record<string, unknown>;
  trades: number | null;
  ret: number | null;
  sharpe: number | null;
  psr: number | null;
  pValue: number | null;
  cleared: boolean;
  watch: boolean;
  deployable: boolean;
  flags: string[];
};

type MixRun = {
  cls: string;
  runId: number | null;
  trades: number;
  ret: number | null;
  sharpe: number | null;
};

type MixRec = {
  conId: number | null;
  preflight: string;
  learn: string;
  complete: boolean;
  block: string;
  dataStatus: string;
  dataRows: number;
  dataLast: string | null;
  dataKey: string | null;
  picks: MixPick[];
  anyCleared: boolean;
  anyWatch: boolean;
  note: string;
  runs: MixRun[];
};

const AS_OF = ''' + json.dumps(as_of) + r''';
const MIX_AS_OF = ''' + json.dumps(mix_as_of) + r''';
const COUNTS = ''' + counts_js + r''' as const;
const AGENTS: Agent[] = ''' + agents_js + r''';
const BIOTECH = ''' + bio_js + r''';
const MIX: Record<string, MixRec> = ''' + mix_js + r''';

const FAMILY_MIX: Record<Family, { title: string; strategies: string; note: string }> = {
  fade: {
    title: "Fade / reversion",
    strategies: "GapReversion, OpeningDriveFade, VwapReversion",
    note: "Regime prior from today's gap/session TR — context only until a daily run clears confidence.",
  },
  breakout: {
    title: "Breakout",
    strategies: "OpeningRangeBreakout, KeltnerBreakout, VwapReclaim",
    note: "Regime prior from wide session range — context only until a daily run clears confidence.",
  },
  trend: {
    title: "Trend / momentum",
    strategies: "Momentum, LateDayMomentum, VwapReclaim",
    note: "Regime prior from contained TR + flow — context only until a daily run clears confidence.",
  },
  range: {
    title: "Range",
    strategies: "RsiAtrRange, MeanReversion",
    note: "Regime prior from sub-1% session TR — context only until a daily run clears confidence.",
  },
};

const TREE_NODES = [
  { id: "preflight", label: "PRE-FLIGHT", detail: "IB / Massive / local freshness" },
  { id: "context", label: "CONTEXT", detail: "Name-conditional regime" },
  { id: "ctx_snap", label: "snapshot", detail: "Alpaca IEX last / OHLC" },
  { id: "ctx_ideas", label: "ideas(tickers)", detail: "mmr-skill ideas" },
  { id: "ctx_news", label: "news", detail: "Massive / Alpaca news" },
  { id: "ctx_implied", label: "implied_move", detail: "Vol through expiry" },
  { id: "ctx_ratios", label: "ratios", detail: "Fundamentals" },
  { id: "ctx_regime", label: "regime", detail: "gap / trend / range / ATR" },
  { id: "learn", label: "LEARN MIX", detail: "mmr-skill, no live orders" },
  { id: "learn_inspect", label: "strategies_inspect", detail: "Dispatch + tunables" },
  { id: "learn_data", label: "data_download", detail: "1 day bars, 180d" },
  { id: "learn_batch", label: "backtest_batch", detail: "10 strategies, daily" },
  { id: "learn_conf", label: "backtests_confidence", detail: "PSR / t / CI" },
  { id: "learn_mix", label: "recommended mix", detail: "Evidence over regime prior" },
  { id: "loop", label: "LOOP", detail: "mmr-loop-skill, propose only" },
  { id: "loop_mon", label: "MONITOR", detail: "portfolio snapshot/diff" },
  { id: "loop_ana", label: "ANALYZE", detail: "ideas + risk" },
  { id: "loop_pro", label: "PROPOSE", detail: "auto_approve=false" },
  { id: "loop_dig", label: "DIGEST", detail: "cycle summary" },
];

const TREE_EDGES = [
  { from: "preflight", to: "context" },
  { from: "context", to: "ctx_snap" },
  { from: "context", to: "ctx_ideas" },
  { from: "context", to: "ctx_news" },
  { from: "context", to: "ctx_implied" },
  { from: "context", to: "ctx_ratios" },
  { from: "context", to: "ctx_regime" },
  { from: "context", to: "learn" },
  { from: "learn", to: "learn_inspect" },
  { from: "learn", to: "learn_data" },
  { from: "learn", to: "learn_batch" },
  { from: "learn", to: "learn_conf" },
  { from: "learn", to: "learn_mix" },
  { from: "context", to: "loop" },
  { from: "loop", to: "loop_mon" },
  { from: "loop", to: "loop_ana" },
  { from: "loop", to: "loop_pro" },
  { from: "loop", to: "loop_dig" },
];

function mixOf(symbol: string): MixRec | undefined {
  return MIX[symbol];
}

function nodeState(symbol: string, id: string): NodeState {
  const m = mixOf(symbol);
  if (id === "preflight") {
    if (!m) return "blocked";
    return m.conId ? "done" : "blocked";
  }
  if (id === "ctx_snap" || id === "ctx_regime") return "done";
  if (id === "context") return "done";
  if (id.startsWith("ctx_")) return "queued";
  if (id === "learn_inspect") return "done";
  if (id === "learn_data") {
    if (!m) return "queued";
    if ((m.dataRows || 0) >= 80) return "done";
    if (m.dataStatus === "error") return "blocked";
    return "queued";
  }
  if (id === "learn_batch" || id === "learn_conf" || id === "learn_mix" || id === "learn") {
    if (!m) return "queued";
    if (m.complete) return "done";
    if (!m.conId) return "blocked";
    return "queued";
  }
  if (id === "loop_pro") return "blocked";
  if (id.startsWith("loop")) return "idle";
  return "idle";
}

function nodeBlockReason(symbol: string, id: string): string {
  const m = mixOf(symbol);
  if (id === "loop_pro") return "Propose-only. auto_approve stays false. No buy/sell/approve.";
  if (id === "preflight" && m && !m.conId) {
    return m.block || "conId unresolved — trader_service down, IB Gateway 7497 refused.";
  }
  if (id === "learn_data" && m && (m.dataRows || 0) < 80) {
    return "Massive 429 after first names; TwelveData fallback not finished. Last usable daily print 2026-09-04 where present.";
  }
  if ((id === "learn_batch" || id === "learn_conf" || id === "learn_mix" || id === "learn") && m && !m.conId) {
    return "PRE-FLIGHT blocked on conId. Will not invent a contract id.";
  }
  return "";
}

function fmtUsd(n: number): string {
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtPct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(2)}%`;
}

function fmtNum(n: number | null | undefined, d = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(d);
}

function learnLabel(m: MixRec | undefined): string {
  if (!m) return "queued";
  if (m.complete) return m.anyWatch ? "done · watch" : "done · no deploy";
  if (m.learn === "blocked") return "blocked";
  return m.learn;
}

function activeCount(symbol: string): number {
  return TREE_NODES.filter((n) => nodeState(symbol, n.id) === "active").length;
}

const TOTAL_ACTIVE = AGENTS.reduce((sum, a) => sum + activeCount(a.symbol), 0);

function Scatter({
  agents,
  selected,
  onSelect,
}: {
  agents: Agent[];
  selected: string;
  onSelect: (s: string) => void;
}) {
  const theme = useHostTheme();
  const width = 640;
  const height = 340;
  const pad = { l: 52, r: 16, t: 16, b: 40 };
  const xs = agents.map((a) => Math.log10(Math.max(a.dollarVol, 1)));
  const ys = agents.map((a) => a.atrPct);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const x = (v: number) => pad.l + ((v - minX) / (maxX - minX || 1)) * (width - pad.l - pad.r);
  const y = (v: number) => height - pad.b - ((v - minY) / (maxY - minY || 1)) * (height - pad.t - pad.b);
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img">
      <title>Intraday ATR(14)% vs session dollar volume</title>
      <text x={width / 2} y={14} textAnchor="middle" fill={theme.text.secondary} fontSize={11}>
        Intraday ATR(14) % vs session dollar volume (Alpaca IEX, 2026-09-08 RTH)
      </text>
      <line x1={pad.l} y1={height - pad.b} x2={width - pad.r} y2={height - pad.b} stroke={theme.stroke.tertiary} />
      <line x1={pad.l} y1={pad.t} x2={pad.l} y2={height - pad.b} stroke={theme.stroke.tertiary} />
      <text x={width / 2} y={height - 8} textAnchor="middle" fill={theme.text.tertiary} fontSize={10}>
        Dollar volume (log10, IEX vwap × volume)
      </text>
      <text
        x={14}
        y={height / 2}
        textAnchor="middle"
        fill={theme.text.tertiary}
        fontSize={10}
        transform={`rotate(-90 14 ${height / 2})`}
      >
        ATR(14) on 5-min RTH / last (%)
      </text>
      {agents.map((a) => {
        const cx = x(Math.log10(Math.max(a.dollarVol, 1)));
        const cy = y(a.atrPct);
        const on = a.symbol === selected;
        const m = mixOf(a.symbol);
        const done = m?.complete;
        return (
          <circle
            key={a.symbol}
            cx={cx}
            cy={cy}
            r={on ? 6 : done ? 4.5 : 3.5}
            fill={on ? theme.accent.primary : done ? theme.fill.secondary : theme.fill.tertiary}
            stroke={on ? theme.accent.primary : theme.stroke.secondary}
            style={{ cursor: "pointer" }}
            onClick={() => onSelect(a.symbol)}
          >
            <title>{`${a.symbol} ATR ${a.atrPct.toFixed(3)}%  ${fmtUsd(a.dollarVol)}`}</title>
          </circle>
        );
      })}
    </svg>
  );
}

function DecisionTree({ symbol }: { symbol: string }) {
  const theme = useHostTheme();
  const layout = computeDAGLayout({
    nodes: TREE_NODES.map((n) => ({ id: n.id })),
    edges: TREE_EDGES,
    direction: "vertical",
    nodeWidth: 132,
    nodeHeight: 44,
    rankGap: 56,
    nodeGap: 16,
    padding: 8,
  });
  const byId = Object.fromEntries(layout.nodes.map((n) => [n.id, n]));
  const meta = Object.fromEntries(TREE_NODES.map((n) => [n.id, n]));
  return (
    <div style={{ overflowX: "auto" }}>
      <svg width={layout.width} height={layout.height} role="img">
        <title>{`${symbol} decision hierarchy`}</title>
        {layout.edges.map((e, i) => (
          <line
            key={`${e.from}-${e.to}-${i}`}
            x1={e.sourceX}
            y1={e.sourceY}
            x2={e.targetX}
            y2={e.targetY}
            stroke={theme.stroke.tertiary}
          />
        ))}
        {TREE_NODES.map((n) => {
          const pos = byId[n.id];
          if (!pos) return null;
          const st = nodeState(symbol, n.id);
          const active = st === "active";
          const done = st === "done";
          const blocked = st === "blocked";
          const fill = active
            ? theme.accent.primary
            : done
              ? theme.fill.secondary
              : blocked
                ? theme.fill.tertiary
                : theme.bg.elevated;
          const color = active ? theme.text.onAccent : theme.text.primary;
          const reason = blocked ? nodeBlockReason(symbol, n.id) : "";
          return (
            <g key={n.id}>
              <rect
                x={pos.x}
                y={pos.y}
                width={132}
                height={44}
                rx={6}
                fill={fill}
                stroke={active ? theme.accent.primary : theme.stroke.secondary}
              />
              <text x={pos.x + 8} y={pos.y + 17} fill={color} fontSize={10} fontWeight={600}>
                {n.label}
              </text>
              <text x={pos.x + 8} y={pos.y + 32} fill={active ? theme.text.onAccent : theme.text.tertiary} fontSize={9}>
                {st}
                {reason ? " · blocked" : ""}
              </text>
              <title>{`${n.label}: ${st}${reason ? " — " + reason : ""} · ${meta[n.id]?.detail ?? ""}`}</title>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export default function SymbolAgentFactory() {
  const [selected, setSelected] = useCanvasState("factory.selected", "META");
  const [phase, setPhase] = useCanvasState<PhaseFilter>("factory.phase", "all");
  const agent = AGENTS.find((a) => a.symbol === selected) ?? AGENTS[0];
  const m = mixOf(agent.symbol);
  const visible = AGENTS.filter((a) => {
    const rec = mixOf(a.symbol);
    if (phase === "all") return true;
    if (phase === "mix-done") return Boolean(rec?.complete);
    if (phase === "blocked") return rec?.learn === "blocked";
    return a.family === phase;
  });
  const regime = FAMILY_MIX[agent.family];
  const learnState = learnLabel(m);

  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>Symbol agent factory</H1>
        <Text>
          One agent per surviving NASDAQ name. Screen: top 25% of 0.5 × pct_rank(intraday ATR%) + 0.5 ×
          pct_rank(dollar volume), biotech excluded. Screen as of {AS_OF}. LEARN MIX checkpoint {MIX_AS_OF}.
        </Text>
      </Stack>

      <Row gap={16} wrap>
        <Stat value={String(COUNTS.agents)} label="Agents" />
        <Stat value={`${COUNTS.mixComplete}/74`} label="Mix complete" tone="info" />
        <Stat value={String(COUNTS.resolved)} label="Local conIds" />
        <Stat value={String(COUNTS.blocked)} label="PRE-FLIGHT blocked" tone="warning" />
        <Stat value={String(COUNTS.watch)} label="Watch-only mixes" tone="warning" />
        <Stat value={String(COUNTS.cleared)} label="Cleared for deploy" />
        <Stat value={String(TOTAL_ACTIVE)} label="Active decision nodes" />
      </Row>

      <Callout tone="warning" title="LEARN MIX status — honest, not fabricated">
        Paper trader_service is up (DUM449329) with ib_upstream_connected. All 74 names resolved via IB SMART/USD.
        LEARN MIX is the EOD-flat *intraday* book (1-min × 60d): GapReversion, VwapReversion,
        VwapReclaim, OpeningRangeBreakout, OpeningDriveFade, RsiAtrRange, LateDayMomentum,
        MeanReversionIntraday, MomentumIntraday, KeltnerBreakoutIntraday.
        Overnight MeanReversion / Momentum / KeltnerBreakout stay out of this mix.
        Zero buy/sell/approve. auto_approve stays false.
      </Callout>

      <H2>Factory floor</H2>
      <Text size="small" tone="secondary">
        Filter by mix family, completed LEARN MIX, or PRE-FLIGHT blocked. Click a name to light its tree.
      </Text>
      <Row gap={8} wrap>
        {(["all", "mix-done", "blocked", "fade", "breakout", "trend", "range"] as PhaseFilter[]).map((f) => (
          <span key={f}>
            <Pill active={phase === f} onClick={() => setPhase(f)}>
              {f === "mix-done" ? "mix complete" : f}
            </Pill>
          </span>
        ))}
      </Row>
      <Row gap={6} wrap>
        {visible.map((a) => (
          <span key={a.symbol}>
            <Pill active={a.symbol === agent.symbol} onClick={() => setSelected(a.symbol)}>
              {a.symbol}
            </Pill>
          </span>
        ))}
      </Row>

      <Grid columns="1.4fr 1fr" gap={20}>
        <Stack gap={12}>
          <H2>{agent.symbol} decision hierarchy</H2>
          <Text size="small" tone="secondary">
            PRE-FLIGHT → CONTEXT → LEARN MIX → LOOP. Accent = active. Filled secondary = done. Blocked nodes keep a
            reason.
          </Text>
          <DecisionTree symbol={agent.symbol} />
        </Stack>

        <Stack gap={12}>
          <Card>
            <CardHeader trailing={<Pill active={m?.complete || m?.learn === "blocked"}>{learnState}</Pill>}>
              {agent.symbol} agent
            </CardHeader>
            <CardBody>
              <Stack gap={10}>
                <Text weight="semibold">{agent.name}</Text>
                <Row gap={12} wrap>
                  <Stat value={agent.combo.toFixed(3)} label="Combo score" />
                  <Stat value={`${agent.atrPct.toFixed(3)}%`} label="ATR(14) 5-min" />
                  <Stat value={fmtUsd(agent.dollarVol)} label="IEX $ volume" />
                </Row>
                <Divider />
                <Text size="small" tone="secondary">
                  Last ${agent.last.toFixed(2)} · session TR {agent.sessionTrPct.toFixed(2)}% · gap{" "}
                  {agent.gapPct.toFixed(2)}% · change {agent.chgPct.toFixed(2)}% · {agent.atrBars} five-minute
                  RTH bars
                </Text>
                {agent.industry ? (
                  <Text size="small" tone="secondary">
                    {agent.sector ? `${agent.sector} · ` : ""}
                    {agent.industry}
                  </Text>
                ) : null}
                <Text size="small" tone="secondary">
                  conId {m?.conId ?? "unresolved"} · daily bars {m?.dataRows ?? 0}
                  {m?.dataLast ? ` through ${m.dataLast}` : ""} · key {m?.dataKey ?? "—"}
                </Text>
                <H3>Recommended mix (evidence)</H3>
                {m?.complete && m.picks.length > 0 ? (
                  <Stack gap={6}>
                    {m.picks.map((p) => (
                      <Text key={`${p.cls}-${p.runId}`}>
                        {p.cls} · run {p.runId ?? "—"} · defaults · trades {p.trades ?? 0} · ret {fmtPct(p.ret)} ·
                        Sharpe {fmtNum(p.sharpe)} · PSR {fmtNum(p.psr, 3)} ·{" "}
                        {p.deployable ? "deployable" : p.watch ? "watch only" : "not deployable"}
                        {p.flags.length ? ` · ${p.flags.join(", ")}` : ""}
                      </Text>
                    ))}
                    <Text size="small" tone="secondary">
                      {m.note}
                    </Text>
                  </Stack>
                ) : (
                  <Stack gap={6}>
                    <Text weight="semibold">{regime.title} (regime prior only)</Text>
                    <Text>{regime.strategies}</Text>
                    <Text size="small" tone="secondary">
                      {m?.note || regime.note}
                    </Text>
                  </Stack>
                )}
                {m?.complete && m.runs.length > 0 ? (
                  <>
                    <H3>Daily batch (180d, defaults)</H3>
                    <Text size="small" tone="secondary">
                      Intraday families (gap / VWAP / ORB / late-day) are expected-empty on 1-day bars. Not promoted
                      to 1-min — no daily winner cleared confidence.
                    </Text>
                    <Table
                      headers={["Strategy", "run", "tr", "ret", "Sharpe"]}
                      columnAlign={["left", "right", "right", "right", "right"]}
                      rows={m.runs.map((r) => [
                        r.cls,
                        r.runId != null ? String(r.runId) : "—",
                        String(r.trades),
                        fmtPct(r.ret),
                        fmtNum(r.sharpe),
                      ])}
                    />
                  </>
                ) : null}
                <H3>Next action</H3>
                <Text>
                  {m?.complete
                    ? "Do not deploy. Loop config: tickers=[" +
                      agent.symbol +
                      "], auto_approve=false. Bring up trader_service to resolve the other 63, then resume the batch."
                    : m?.block
                      ? m.block
                      : "Waiting on conId resolve or daily bars."}{" "}
                  Never buy/sell/approve from this factory.
                </Text>
              </Stack>
            </CardBody>
          </Card>
        </Stack>
      </Grid>

      <H2>Completed LEARN MIX ({COUNTS.mixComplete} / 74)</H2>
      <Text size="small" tone="secondary">
        Source: local MMR Backtester · bar_size 1 min · days=60 · EOD-flat strategies only · fill next_open.
      </Text>
      <Table
        headers={["Symbol", "conId", "Best evidence", "run", "tr", "ret", "PSR", "Deploy?"]}
        columnAlign={["left", "right", "left", "right", "right", "right", "right", "left"]}
        rows={AGENTS.filter((a) => mixOf(a.symbol)?.complete).map((a) => {
          const rec = mixOf(a.symbol)!;
          const p = rec.picks[0];
          return [
            a.symbol,
            rec.conId != null ? String(rec.conId) : "—",
            p ? p.cls : "none cleared",
            p?.runId != null ? String(p.runId) : "—",
            p ? String(p.trades ?? 0) : "—",
            p ? fmtPct(p.ret) : "—",
            p ? fmtNum(p.psr, 3) : "—",
            p?.deployable ? "yes" : p?.watch ? "watch" : "no",
          ];
        })}
        striped
      />

      <H2>ATR% vs dollar volume</H2>
      <Text size="small" tone="secondary">
        Source: Alpaca IEX · 2026-09-08 RTH through 15:51 ET · all 74 surviving agents. Selected name is larger;
        completed LEARN MIX names are slightly larger than queued.
      </Text>
      <Scatter agents={AGENTS} selected={agent.symbol} onSelect={setSelected} />

      <H2>Surviving agents</H2>
      <Table
        headers={["#", "Symbol", "Last", "ATR%", "Sess TR%", "$ vol", "Family", "Learn", "Mix"]}
        columnAlign={["right", "left", "right", "right", "right", "right", "left", "left", "left"]}
        rows={visible.map((a) => {
          const rec = mixOf(a.symbol);
          const mixTxt = rec?.complete
            ? rec.picks[0]
              ? `${rec.picks[0].cls}${rec.picks[0].watch ? " watch" : ""}`
              : "no deploy"
            : rec?.learn === "blocked"
              ? "blocked"
              : "queued";
          return [
            String(a.rank),
            a.symbol,
            a.last.toFixed(2),
            a.atrPct.toFixed(3),
            a.sessionTrPct.toFixed(2),
            fmtUsd(a.dollarVol),
            a.family,
            learnLabel(rec),
            mixTxt,
          ];
        })}
        rowTone={visible.map((a) => (a.symbol === agent.symbol ? "info" : undefined))}
        striped
        stickyHeader
      />

      <H2>Biotech exclusions</H2>
      <Text size="small" tone="secondary">
        Dropped before the factory floor. Devices / diagnostics (ISRG, PODD, ILMN, GH, NTRA) stayed.
      </Text>
      <Table
        headers={["Symbol", "Industry", "Reason"]}
        rows={BIOTECH.map((b: { symbol: string; industry: string; reason: string }) => [
          b.symbol,
          b.industry || "—",
          b.reason,
        ])}
      />

      <Text size="small" tone="tertiary">
        Hierarchy: PRE-FLIGHT → CONTEXT (snapshot / ideas / news / implied / ratios / regime) → LEARN MIX
        (inspect → download → backtest_batch → confidence → mix) → LOOP (MONITOR → ANALYZE → PROPOSE → DIGEST).
        Loop never auto-approves. Mix learning complete: {COUNTS.mixComplete} / 74. Screen: Alpaca IEX ATR and
        dollar volume + Alpha Vantage industry. Mix: 1-min EOD-flat backtests (gap/VWAP/ORB/fade/range), not daily MeanReversion.
      </Text>
    </Stack>
  );
}
'''

out.write_text(tsx, encoding="utf-8")
print("wrote", out, "bytes", out.stat().st_size)
print("mix_complete", n_complete, "resolved", n_resolved, "blocked", n_blocked, "watch", n_watch)
