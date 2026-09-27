'use strict';
const content = document.getElementById('content');
let wordNodes = [];
let activeWord = null;
let activeRow = null;

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
    wordNodes.push(cursor < visible.length ? visible[cursor++] : null);
  }
}

window.readerRender = function(markup, theme, spokenWords) {
  clearHighlight();
  content.innerHTML = markup;
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
