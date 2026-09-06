(() => {
  const form = document.querySelector("[data-allocation-form]");
  if (!form) return;

  const inputs = [...form.querySelectorAll("[data-allocation-input]")];
  const total = document.querySelector("[data-allocation-total]");
  const totalWrap = document.querySelector("[data-allocation-total-wrap]");
  const validation = form.querySelector("[data-allocation-validation]");
  const submit = form.querySelector("[data-allocation-submit]");

  const update = () => {
    const values = inputs.map((input) => {
      const value = Number.parseFloat(input.value);
      return Number.isFinite(value) && value >= 0 ? value : 0;
    });
    const sum = values.reduce((result, value) => result + value, 0);
    const fieldsValid = inputs.every((input) => input.validity.valid);
    const valid = fieldsValid && Math.abs(sum - 100) <= 0.01;

    total.textContent = `${sum.toFixed(1)}%`;
    totalWrap.classList.toggle("is-invalid", !valid);
    validation.classList.toggle("is-invalid", !valid);
    if (valid) {
      validation.textContent = "比例合计正确";
    } else if (!fieldsValid) {
      validation.textContent = "单项比例需在 0% 到 100% 之间";
    } else if (sum < 100) {
      validation.textContent = `还需分配 ${(100 - sum).toFixed(1)}%`;
    } else {
      validation.textContent = `已超出 ${(sum - 100).toFixed(1)}%`;
    }
    submit.disabled = !valid;

    inputs.forEach((input, index) => {
      const asset = input.dataset.allocationInput;
      const segment = form.querySelector(`[data-allocation-segment="${asset}"]`);
      const share = sum > 0 ? (values[index] / sum) * 100 : 0;
      segment.style.width = `${share}%`;
      segment.title = `${input.closest("label").querySelector("strong").textContent} ${values[index].toFixed(1)}%`;
    });
  };

  inputs.forEach((input) => input.addEventListener("input", update));
  update();
})();
