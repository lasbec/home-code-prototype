const format = new Intl.NumberFormat('de-DE', { maximumFractionDigits: 2 });

for (const model of document.querySelectorAll('[data-ir-model]')) {
  const power = model.querySelector('[name="power"]');
  const distance = model.querySelector('[name="distance"]');
  const threshold = model.querySelector('[name="threshold"]');
  const signalMeter = model.querySelector('[data-signal]');
  const thresholdMeter = model.querySelector('[data-threshold]');
  const result = model.querySelector('output');

  function update() {
    const signal = Number(power.value) / Number(distance.value) ** 2;
    const required = Number(threshold.value);
    const margin = signal / required;
    signalMeter.value = signal;
    thresholdMeter.value = required;
    result.textContent = `Signal ${format.format(signal)} · Schwelle ${format.format(required)} · ` +
      `Signal relativ zur Schwelle ${format.format(margin)}×. ` +
      (margin > 1 ? 'Über der Schwelle.' : margin === 1 ? 'Gerade an der Schwelle.' : 'Unter der Schwelle.');
  }

  model.addEventListener('change', update);
  update();
}
