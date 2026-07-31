async function refreshBuildPage() {
  const meta = window.STORYFORGE_BUILD_PAGE;
  if (!meta) return;
  try {
    const status = await fetch(`/projects/${meta.slug}/status`, { cache: 'no-store' }).then(r => r.json());
    const log = await fetch(`/projects/${meta.slug}/log`, { cache: 'no-store' }).then(r => r.json());
    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value ?? '—';
    };
    setText('job-status', status.status);
    setText('job-stage', status.stage);
    setText('job-chapter', status.current_chapter ?? '—');
    setText('job-total', status.total_chapters ?? '—');
    setText('job-message', status.message ?? '—');
    setText('job-started', status.started_at ?? '—');
    setText('job-finished', status.finished_at ?? '—');
    const logEl = document.getElementById('build-log');
    if (logEl && Array.isArray(log.lines)) {
      logEl.textContent = log.lines.join('
');
    }
  } catch (error) {
    console.error(error);
  }
}
setInterval(refreshBuildPage, 2000);
window.addEventListener('load', refreshBuildPage);
