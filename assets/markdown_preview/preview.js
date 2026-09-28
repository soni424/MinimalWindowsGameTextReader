'use strict';
const content = document.getElementById('content');
let wordNodes = [];
let activeWord = null;
let activeRow = null;
let documentRevision = 0;

function clearHighlight() {
  if (activeWord) activeWord.classList.remove('speech-word');
  if (activeRow) activeRow.classList.remove('speech-row');
  activeWord = activeRow = null;
}

function collectWords(spokenWords) {
  const textNodes = [];
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.parentElement?.closest('pre,code,.math-inline,.math-block,.footnote-ref,.footnote-backref,.md-image-blocked'))
        return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  const visible = [];
  const expression = /[\p{L}\p{N}_]+(?:[-'’][\p{L}\p{N}_]+)*/gu;
  for (const node of textNodes) {
    if (node.parentElement?.classList.contains('ordered-marker')) {
      for (const label of (node.parentElement.dataset.spokenLabel || '').matchAll(expression))
        visible.push({element:node.parentElement, text:label[0].toLocaleLowerCase()});
      continue;
    }
    const fragment = document.createDocumentFragment();
    let position = 0;
    for (const match of node.textContent.matchAll(expression)) {
      fragment.appendChild(document.createTextNode(node.textContent.slice(position,match.index)));
      const word = document.createElement('span');
      word.textContent = match[0];
      fragment.appendChild(word);
      visible.push({element:word, text:match[0].toLocaleLowerCase()});
      position = match.index + match[0].length;
    }
    fragment.appendChild(document.createTextNode(node.textContent.slice(position)));
    node.replaceWith(fragment);
  }
  wordNodes = [];
  let cursor = 0;
  for (const value of spokenWords) {
    const target = String(value).toLocaleLowerCase();
    while (cursor < visible.length && visible[cursor].text !== target) cursor++;
    const item = cursor < visible.length ? visible[cursor++] : null;
    if (item && item.element.dataset.readerWordIndex === undefined)
      item.element.dataset.readerWordIndex = String(wordNodes.length);
    wordNodes.push(item);
  }
}

function insertOrderedMarkers() {
  const names = ['zero','one','two','three','four','five','six','seven','eight','nine'];
  function cardinal(n) {
    if (n < 10) return names[n];
    const teens = ['ten','eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen'];
    if (n < 20) return teens[n-10];
    const tens = ['','','twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety'];
    if (n < 100) return tens[Math.floor(n/10)] + (n%10 ? '-' + names[n%10] : '');
    return names[Math.floor(n/100)] + ' hundred' + (n%100 ? ' ' + cardinal(n%100) : '');
  }
  content.querySelectorAll('ol').forEach(list => {
    let value = Number(list.getAttribute('start') || 1);
    for (const item of list.children) {
      if (item.tagName !== 'LI') continue;
      if (item.hasAttribute('value')) value = Number(item.getAttribute('value'));
      if (!Number.isInteger(value) || value < 0 || value > 999) continue;
      const marker = document.createElement('span');
      marker.className = 'ordered-marker';
      marker.dataset.spokenLabel = cardinal(value);
      marker.textContent = value + '.';
      item.prepend(marker);
      value++;
    }
  });
}

window.readerRender = function(markup, theme, spokenWords, revision) {
  clearHighlight();
  content.innerHTML = markup;
  documentRevision = revision;
  insertOrderedMarkers();
  document.body.className = theme === 'light' ? 'light' : 'dark';
  document.documentElement.style.colorScheme = theme === 'light' ? 'light' : 'dark';
  for (const element of content.querySelectorAll('.math-inline,.math-block')) {
    try { katex.render(element.dataset.tex || '', element, {
      displayMode:element.classList.contains('math-block'), trust:false,
      throwOnError:false, maxExpand:1000, maxSize:20
    }); } catch (_error) { element.textContent = '[Equation could not be displayed]'; }
  }
  collectWords(spokenWords || []);
};

content.addEventListener('dblclick', event => {
  const index = event.target.closest('[data-reader-word-index]')?.dataset.readerWordIndex;
  if (index === undefined || !window.ipc?.postMessage) return;
  window.ipc.postMessage(JSON.stringify({kind:'seek', index:Number(index), revision:documentRevision}));
});

window.readerLoadImages = function(urls) {
  content.querySelectorAll('.md-image[data-image-index]').forEach(element => {
    const index = Number(element.dataset.imageIndex);
    const url = urls[index];
    if (typeof url !== 'string' || !url.startsWith('https://')) return;
    const img = document.createElement('img');
    img.alt = element.textContent;
    img.referrerPolicy = 'no-referrer';
    img.loading = 'lazy';
    img.onerror = () => { img.remove(); element.classList.remove('loaded'); };
    img.src = url;
    element.appendChild(img);
    element.classList.add('loaded');
  });
};

window.readerHighlight = function(index) {
  clearHighlight();
  const item = wordNodes[index];
  if (!item) return;
  activeWord = item.element;
  const details = activeWord.closest('details:not([open])');
  if (details) details.open = true;
  activeWord.classList.add('speech-word');
  activeWord.scrollIntoView({block:'nearest'});
};
window.readerClearHighlight = clearHighlight;
