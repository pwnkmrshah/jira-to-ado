import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';

const TERMINAL = new Set(['completed', 'warning', 'failed', 'cancelled']);

/** Starts a job via `starter()` (must resolve to { job_id, status }) and polls
 * /status/<job_id> every 2s until it reaches a terminal state. */
export function useJob() {
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [startError, setStartError] = useState('');
  const timerRef = useRef(null);

  const reset = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    setJobId(null);
    setJob(null);
    setStartError('');
  };

  const start = async (starter) => {
    reset();
    try {
      const result = await starter();
      setJobId(result.job_id);
      setJob({ status: result.status || 'queued' });
    } catch (err) {
      setStartError(err.message);
    }
  };

  useEffect(() => {
    if (!jobId) return undefined;

    const poll = async () => {
      try {
        const result = await api.status(jobId);
        setJob(result);
        if (TERMINAL.has(result.status) && timerRef.current) {
          clearInterval(timerRef.current);
        }
      } catch {
        // transient network error — keep polling
      }
    };

    poll();
    timerRef.current = setInterval(poll, 2000);
    return () => clearInterval(timerRef.current);
  }, [jobId]);

  const isRunning = !!jobId && !!job && !TERMINAL.has(job.status);

  return { jobId, job, startError, isRunning, start, reset };
}
