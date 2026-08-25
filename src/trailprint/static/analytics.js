const chart = document.querySelector('#created-line-chart');
const payload = document.querySelector('#created-chart-data');

if (chart && payload) {
  const points = JSON.parse(payload.textContent);
  const width = 900;
  const height = 320;
  const margin = { top: 24, right: 24, bottom: 48, left: 48 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const maxCount = Math.max(1, ...points.map(item => item.count));
  const x = index => margin.left + (points.length === 1 ? innerWidth / 2 : index / (points.length - 1) * innerWidth);
  const y = count => margin.top + innerHeight - count / maxCount * innerHeight;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'group');
  svg.setAttribute('aria-label', 'Projects created by day chart');

  const add = (name, attributes, text) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    svg.appendChild(node);
    return node;
  };

  const tickCount = Math.min(4, maxCount);
  for (let tick = 0; tick <= tickCount; tick += 1) {
    const value = Math.round(tick / tickCount * maxCount);
    const tickY = y(value);
    add('line', { x1: margin.left, x2: width - margin.right, y1: tickY, y2: tickY, class: 'chart-grid' });
    add('text', { x: margin.left - 10, y: tickY + 4, class: 'chart-axis-label', 'text-anchor': 'end' }, value);
  }
  const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
  labelIndexes.forEach(index => add('text', {
    x: x(index), y: height - 16, class: 'chart-axis-label', 'text-anchor': 'middle'
  }, points[index].date));

  const path = points.map((item, index) => `${index ? 'L' : 'M'} ${x(index)} ${y(item.count)}`).join(' ');
  add('path', { d: path, class: 'chart-line' });
  points.forEach((item, index) => {
    const point = add('circle', { cx: x(index), cy: y(item.count), r: 5, class: 'chart-point',
      tabindex: 0, role: 'img', 'aria-label': `${item.date}: ${item.count} project${item.count === 1 ? '' : 's'} created` });
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = `${item.date}: ${item.count}`;
    point.appendChild(title);
  });
  chart.appendChild(svg);
}
