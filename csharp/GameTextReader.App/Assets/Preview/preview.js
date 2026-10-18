'use strict';
let revision = 0;
let highlightSequence = -1, activeRequest = 0, activePlayback = 0, activeWord = null, rowFrame = 0;
const content = document.getElementById('content');
window.readerRender = (html, theme, currentRevision) => {
  if (!Number.isSafeInteger(currentRevision) || currentRevision < revision) return false;
  clearHighlight();
  if (currentRevision !== revision) { highlightSequence = -1; activeRequest = 0; activePlayback = 0; }
  content.innerHTML = html;
  revision = currentRevision;
  document.body.className = theme === 'light' ? 'light' : 'dark';
  document.documentElement.style.colorScheme = theme === 'light' ? 'light' : 'dark';
  for (const element of content.querySelectorAll('.math-inline,.math-block')) {
    try { katex.render(element.dataset.tex || '', element, {displayMode: element.classList.contains('math-block'),trust:false,throwOnError:false,maxExpand:1000,maxSize:20}); }
    catch (_) { element.textContent = '[Equation could not be displayed]'; }
  }
  return true;
};
const clearHighlight = () => { activeWord?.classList.remove('speech-word'); activeWord = null; document.getElementById('speech-row')?.remove(); };
const updateRow = () => {
  rowFrame = 0;
  if (!activeWord?.isConnected) return;
  const rect = activeWord.getBoundingClientRect(); let row = document.getElementById('speech-row');
  if (!row) { row = document.createElement('div'); row.id = 'speech-row'; row.setAttribute('aria-hidden', 'true'); document.body.appendChild(row); }
  Object.assign(row.style,{top:rect.top+'px',height:rect.height+'px'});
};
window.readerClearHighlight = (documentRevision = revision) => { if (documentRevision === revision) clearHighlight(); };
window.readerHighlight = (index, documentRevision = revision, request = activeRequest, playback = activePlayback, sequence = highlightSequence + 1) => {
  if (documentRevision !== revision || !Number.isSafeInteger(index) || !Number.isSafeInteger(sequence) || sequence < highlightSequence || (request === activeRequest && playback < activePlayback)) return false;
  highlightSequence = sequence; activeRequest = request; activePlayback = playback;
  clearHighlight();
  if (index < 0) return true;
  const word = content.querySelector('[data-reader-word-index="' + index + '"]');
  if (!word) return false;
  const details = word.closest('details:not([open])'); if (details) details.open = true;
  activeWord = word; word.classList.add('speech-word'); word.scrollIntoView({block:'nearest'}); updateRow(); return true;
};
const scheduleRow = () => { if (!rowFrame) rowFrame = requestAnimationFrame(updateRow); };
window.addEventListener('scroll', scheduleRow, {passive:true}); window.addEventListener('resize', scheduleRow);
window.readerLoadImages = urls => {
  content.querySelectorAll('.md-image[data-image-index]').forEach(e => {
    const url = urls[Number(e.dataset.imageIndex)];
    if (typeof url !== 'string' || !url.startsWith('https://')) return;
    if (e.classList.contains('loaded')) return;
    const image = document.createElement('img'); image.alt = e.textContent; image.referrerPolicy = 'no-referrer'; image.loading = 'lazy'; image.src = url;
    e.appendChild(image); e.classList.add('loaded');
  });
};
content.addEventListener('dblclick', e => {
  const word = e.target.closest('[data-reader-word-index]');
  if (!word) return;
  const index = Number(word.dataset.readerWordIndex);
  if (Number.isInteger(index) && index >= 0) window.chrome.webview.postMessage({kind:'seek',index,revision});
});
