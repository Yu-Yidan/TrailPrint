const SVG_NS = 'http://www.w3.org/2000/svg';
const palette = ['#c94f28', '#237a45', '#315b9c', '#8a4b9f'];

function renderLineChart(chartId, payloadId, seriesInput, ariaLabel) {
  const chart = document.querySelector(chartId);
  const payload = document.querySelector(payloadId);
  if (!chart || !payload) return;
  const raw = JSON.parse(payload.textContent);
  const series = seriesInput(raw);
  if (!series.length || !series.some(item => item.points.length)) return;
  const dates = series.find(item => item.points.length).points.map(item => item.date);
  const width = 900, height = 320;
  const margin = { top: 24, right: 24, bottom: 48, left: 48 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const maxCount = Math.max(1, ...series.flatMap(item => item.points.map(point => point.count)));
  const x = index => margin.left + (dates.length === 1 ? innerWidth / 2 : index / (dates.length - 1) * innerWidth);
  const y = count => margin.top + innerHeight - count / maxCount * innerHeight;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'group');
  svg.setAttribute('aria-label', ariaLabel);
  const add = (name, attributes, text) => {
    const node = document.createElementNS(SVG_NS, name);
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
  [...new Set([0, Math.floor((dates.length - 1) / 2), dates.length - 1])].forEach(index => add('text', {
    x: x(index), y: height - 16, class: 'chart-axis-label', 'text-anchor': 'middle'
  }, dates[index]));
  series.forEach((item, seriesIndex) => {
    const color = palette[seriesIndex % palette.length];
    const path = item.points.map((point, index) => `${index ? 'L' : 'M'} ${x(index)} ${y(point.count)}`).join(' ');
    add('path', { d: path, class: 'chart-line', style: `stroke:${color}` });
    item.points.forEach((point, index) => {
      const circle = add('circle', { cx: x(index), cy: y(point.count), r: 5,
        class: 'chart-point', style: `stroke:${color}`, tabindex: 0, role: 'img',
        'aria-label': `${item.name}, ${point.date}: ${point.count}` });
      const title = document.createElementNS(SVG_NS, 'title');
      title.textContent = `${item.name}, ${point.date}: ${point.count}`;
      circle.appendChild(title);
    });
  });
  chart.appendChild(svg);
}

renderLineChart('#created-line-chart', '#created-chart-data', points => [
  { name: 'Projects created', points }
], 'Projects created by day chart');

renderLineChart('#product-line-chart', '#product-chart-data', values => values.map(item => ({
  name: item.dim_product_name, points: item.points
})), 'Cumulative products by product name chart');
