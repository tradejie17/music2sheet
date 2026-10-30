// Transport: plays the original recording, the transcribed score (sampled piano),
// or both in sync. The playhead (ticks) is derived from whichever clock is master:
// the <audio> element when the recording is audible, else the score player's clock.
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { urls } from "../api/client";
import type { TimeMap } from "./music";
import { type Clock, ScorePlayer, SteadyClock } from "./scorePlayer";
import { useEditor } from "./store";

export interface TransportHandle {
  seekTick: (tick: number) => void;
  toggle: () => void;
}

export type PlayMode = "audio" | "synth" | "mix" | "split";
type Timing = "recording" | "steady";

const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, "0")}`;

// One AudioContext per page; the <audio> element is routed through it so that the
// recording can be panned / mixed against the synth.
let shared: { ctx: AudioContext; player: ScorePlayer; recGain: GainNode; recPan: StereoPannerNode; synPan: StereoPannerNode } | null =
  null;
const mediaSources = new WeakMap<HTMLAudioElement, MediaElementAudioSourceNode>();
// where the transport stood when the editor last unmounted (a rerun remounts it), in
// recording seconds, which a rerun does not change (ticks do: tempo ×2 doubles them)
let resumeAt: { jobId: string; sec: number } | null = null;

/** Seconds between the audio context's clock and the speakers (what the ear lags behind). */
function outputLag(ctx: AudioContext): number {
  return (ctx.baseLatency || 0) + (ctx.outputLatency || 0);
}

function engine() {
  if (!shared) {
    const ctx = new AudioContext({ latencyHint: "playback" });
    const pitches = [...new Set(useEditor.getState().doc?.notes.map((n) => n.pitch) ?? [])];
    const player = new ScorePlayer(ctx, pitches.length ? pitches : undefined);
    const synPan = ctx.createStereoPanner();
    player.bus.disconnect();
    player.bus.connect(synPan).connect(ctx.destination);
    const recPan = ctx.createStereoPanner();
    const recGain = ctx.createGain();
    recGain.connect(recPan).connect(ctx.destination);
    shared = { ctx, player, recGain, recPan, synPan };
    Object.assign(window, { __m2s: shared, __editor: useEditor }); // test hook
  }
  return shared;
}

function routeAudio(a: HTMLAudioElement) {
  const e = engine();
  if (!mediaSources.has(a)) {
    const src = e.ctx.createMediaElementSource(a);
    src.connect(e.recGain);
    mediaSources.set(a, src);
  }
  return e;
}

export const Transport = forwardRef<TransportHandle, { jobId: string; time: TimeMap }>(function Transport(
  { jobId, time },
  ref,
) {
  const audio = useRef<HTMLAudioElement>(null);
  const [sec, setSec] = useState(0);
  const [audioDur, setAudioDur] = useState(0);
  const [rate, setRate] = useState(1);
  const [mode, setMode] = useState<PlayMode>("audio");
  const [timing, setTiming] = useState<Timing>("recording");
  const [loading, setLoading] = useState<string | null>(null);
  const playing = useEditor((s) => s.playing);
  const doc = useEditor((s) => s.doc);
  const follow = useEditor((s) => s.follow);
  const { setPlayhead, setPlaying, setFollow } = useEditor.getState();

  const synthOnly = mode === "synth";
  const recAudible = !synthOnly;
  const steady = synthOnly && timing === "steady";
  const clock: Clock = useMemo(
    () => (steady && doc ? new SteadyClock(doc.tempos[0]?.bpm ?? 100, doc.ppq) : time),
    [steady, doc?.tempos, doc?.ppq, time], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const clockRef = useRef(clock);
  clockRef.current = clock;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const timeRef = useRef(time);
  timeRef.current = time;
  const rateRef = useRef(rate);
  rateRef.current = rate;
  // Transport position in seconds of the current clock. It is the truth while stopped
  // (the synth engine may not exist yet: it is created on the first synth/mix play) and
  // follows the playing clock while playing.
  const posRef = useRef(resumeAt?.jobId === jobId ? resumeAt.sec : 0);
  const floorRef = useRef(0); // where playback (re)started: the latency-compensated marker never goes before it

  /** Position now: the running clock while playing, else the stored position. */
  const livePos = (m: PlayMode = modeRef.current) => {
    if (!useEditor.getState().playing) return posRef.current;
    if (m === "synth") return shared?.player.isRunning ? shared.player.position() : posRef.current;
    return audio.current?.currentTime ?? posRef.current;
  };
  /** The clocks run ahead of the speakers by the output latency: the part that is heard now. */
  const heardPos = (pos: number, m: PlayMode = modeRef.current) => {
    const a = audio.current;
    let lag = 0;
    if (m === "synth") lag = shared ? outputLag(shared.ctx) * rateRef.current : 0;
    else if (a && shared && mediaSources.has(a)) lag = outputLag(shared.ctx) * a.playbackRate;
    // never before where playback (re)started
    return Math.max(pos - lag, Math.min(pos, floorRef.current));
  };

  // keep the synth's event list in sync with edits (also while playing)
  useEffect(() => {
    if (doc && shared) shared.player.setScore(doc, clock);
  }, [doc, clock]);

  const dur = synthOnly && shared ? Math.max(audioDur, shared.player.lastEnd) : audioDur;

  const ensureSynth = async () => {
    const e = engine();
    const d = useEditor.getState().doc;
    if (d) e.player.setScore(d, clockRef.current);
    if (e.ctx.state !== "running") await e.ctx.resume();
    if (e.player.loadProgress.loaded < e.player.loadProgress.total || !e.player.loadProgress.total) {
      setLoading("加载钢琴音色…");
      await e.player.ready;
      setLoading(null);
    }
    return e;
  };

  const play = async () => {
    const a = audio.current;
    if (!a) return;
    if (modeRef.current === "audio") {
      if (shared) await shared.ctx.resume();
      if (Math.abs(a.currentTime - posRef.current) > 0.005) a.currentTime = posRef.current;
      void a.play();
      return;
    }
    const e = await ensureSynth();
    if (modeRef.current === "synth") {
      e.player.start(posRef.current, rate);
      setPlaying(true);
    } else {
      routeAudio(a);
      if (Math.abs(a.currentTime - posRef.current) > 0.005) a.currentTime = posRef.current;
      void a.play(); // synth starts on the 'playing' event, anchored to the element's clock
    }
  };
  const pause = (m: PlayMode = modeRef.current) => {
    if (useEditor.getState().playing) {
      // resume from what was last heard, and leave the marker exactly there
      posRef.current = heardPos(livePos(m), m);
      if (m === modeRef.current) {
        setSec(posRef.current);
        setPlayhead(clockRef.current.secToTick(posRef.current));
      }
    }
    audio.current?.pause();
    shared?.player.stop();
    // set here, not only in <audio onPause>: that event arrives asynchronously and is
    // ignored if the mode switched to synth in the meantime (left the button stuck on 暂停)
    setPlaying(false);
  };
  const seekPos = (pos: number) => {
    const a = audio.current;
    pos = Math.max(0, pos);
    posRef.current = pos;
    floorRef.current = pos;
    if (modeRef.current === "synth") shared?.player.cue(pos); // no engine yet: play() starts at posRef
    else if (a) a.currentTime = pos; // synth (if any) re-anchors on 'seeked'
    else shared?.player.cue(pos);
    setSec(pos);
    setPlayhead(clockRef.current.secToTick(pos));
  };

  useImperativeHandle(ref, () => ({
    seekTick: (tick) => seekPos(clockRef.current.tickToSec(tick)),
    toggle: () => (useEditor.getState().playing ? pause() : void play()),
  }));

  // playhead loop + drift correction (mix modes)
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let frames = 0;
    floorRef.current = posRef.current;
    const loop = () => {
      const a = audio.current;
      const p = shared?.player;
      let pos: number;
      if (modeRef.current === "synth" && p) {
        pos = p.position();
        if (pos > p.lastEnd + 1) pause();
      } else {
        pos = a?.currentTime ?? 0;
        // the element's clock is master; nudge the synth when it drifts > 30 ms
        if (p?.isRunning && a && !a.paused && ++frames % 15 === 0 && Math.abs(p.position() - a.currentTime) > 0.03)
          p.reanchor(a.currentTime);
      }
      if (!useEditor.getState().playing) return; // pause() above
      posRef.current = pos;
      const heard = heardPos(pos);
      setSec(heard);
      setPlayhead(clockRef.current.secToTick(heard));
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [playing, setPlayhead]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (audio.current) audio.current.playbackRate = rate;
    shared?.player.setRate(rate);
  }, [rate]);

  // mode/timing change: stop everything, set the mix, carry the position over (via ticks,
  // since recording seconds and steady-tempo seconds differ)
  const prev = useRef<{ mode: PlayMode; clock: Clock }>({ mode, clock });
  useEffect(() => {
    const was = prev.current;
    pause(was.mode);
    const pos = Math.max(0, clock.tickToSec(was.clock.secToTick(posRef.current)));
    prev.current = { mode, clock };
    if (shared) {
      shared.recGain.gain.value = recAudible ? 1 : 0;
      shared.player.bus.gain.value = mode === "audio" ? 0 : mode === "mix" ? 0.8 : 1;
      shared.recPan.pan.value = mode === "split" ? -1 : 0;
      shared.synPan.pan.value = mode === "split" ? 1 : 0;
    }
    posRef.current = pos;
    if (audio.current) audio.current.currentTime = pos;
    shared?.player.cue(pos);
    setSec(pos);
    setPlayhead(clock.secToTick(pos));
  }, [mode, timing]); // eslint-disable-line react-hooks/exhaustive-deps

  // a new tempo map (edit, rerun) moves the paused marker to the same moment of the music
  useEffect(() => {
    if (!useEditor.getState().playing) setPlayhead(clock.secToTick(posRef.current));
  }, [clock, setPlayhead]);

  // unmount (e.g. a rerun replaces the editor): stop, and remember where we were
  useEffect(() => {
    const el = audio.current; // the ref is already detached when this cleanup runs
    return () => {
      const pos = livePos();
      const c = clockRef.current;
      resumeAt = { jobId, sec: c === timeRef.current ? pos : timeRef.current.tickToSec(c.secToTick(pos)) };
      el?.pause();
      shared?.player.stop();
      setPlaying(false);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex items-center gap-3 border-t bg-slate-50 px-3 py-1.5 text-xs">
      <audio
        ref={audio}
        src={urls.audio(jobId)}
        preload="auto"
        onPlay={() => setPlaying(true)}
        onPlaying={(e) => {
          if (modeRef.current !== "audio" && modeRef.current !== "synth" && shared)
            shared.player.start(e.currentTarget.currentTime, e.currentTarget.playbackRate);
        }}
        onSeeked={(e) => {
          const a = e.currentTarget;
          if (!a.paused && shared?.player.isRunning) shared.player.start(a.currentTime, a.playbackRate);
        }}
        onPause={() => {
          if (modeRef.current !== "synth") {
            shared?.player.stop();
            setPlaying(false);
          }
        }}
        onLoadedMetadata={(e) => setAudioDur(e.currentTarget.duration)}
      />
      <button
        className="w-16 rounded bg-slate-800 py-1 text-white hover:bg-slate-700"
        onClick={() => (playing ? pause() : void play())}
        title="空格"
        data-testid="play"
      >
        {playing ? "❚❚ 暂停" : "▶ 播放"}
      </button>
      <span className="tabular-nums text-slate-600">
        {fmt(sec)} / {fmt(dur)}
      </span>
      <input
        type="range"
        className="min-w-0 flex-1"
        min={0}
        max={dur || 1}
        step={0.01}
        value={sec}
        onChange={(e) => seekPos(Number(e.target.value))}
      />
      <select
        className="rounded border px-1"
        value={mode}
        onChange={(e) => setMode(e.target.value as PlayMode)}
        data-testid="mode"
        title="播放内容"
      >
        <option value="audio">原始录音</option>
        <option value="synth">乐谱合成</option>
        <option value="mix">叠加对照</option>
        <option value="split">左原音 / 右乐谱</option>
      </select>
      {synthOnly && (
        <select
          className="rounded border px-1"
          value={timing}
          onChange={(e) => setTiming(e.target.value as Timing)}
          data-testid="timing"
          title="合成的节奏"
        >
          <option value="recording">跟随录音速度</option>
          <option value="steady">按谱面匀速</option>
        </select>
      )}
      <label className="flex items-center gap-1 text-slate-600">
        速度
        <select className="rounded border px-1" value={rate} onChange={(e) => setRate(Number(e.target.value))}>
          {[0.5, 0.75, 1].map((r) => (
            <option key={r} value={r}>
              {r}×
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-1 text-slate-600" title="播放时在乐谱上标出当前位置并自动翻页">
        <input
          type="checkbox"
          checked={follow}
          onChange={(e) => setFollow(e.target.checked)}
          data-testid="follow"
        />
        跟随乐谱
      </label>
      {loading && <span className="text-amber-600">{loading}</span>}
    </div>
  );
});
