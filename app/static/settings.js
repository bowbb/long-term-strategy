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

(() => {
  const form = document.querySelector("[data-housing-form]");
  if (!form) return;
  const enabled = form.querySelector("[data-housing-enabled]");
  const month = form.querySelector("[data-housing-month]");
  const amount = form.querySelector("[data-housing-amount]");
  const preview = form.querySelector("[data-housing-preview]");
  const money = (value) => value.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const update = () => {
    month.required = enabled.checked;
    amount.required = enabled.checked;
    amount.min = enabled.checked ? "0.01" : "0";
    if (!enabled.checked) {
      preview.textContent = "未启用提取安排，计算页按普通投资策略显示和计算。";
      return;
    }
    const value = Number(amount.value);
    if (!month.validity.valid || !month.value || !amount.validity.valid || !Number.isFinite(value) || value <= 0) {
      preview.textContent = "设置买房年月和预算后，这里会显示首次与每月的提取金额。";
      return;
    }
    const year = Number(month.value.slice(0, 4));
    const monthNumber = Number(month.value.slice(5));
    const firstCents = Math.floor(Math.round(value * 100) / 2);
    const monthly = (Math.round(value * 100) - firstCents) / 1200;
    preview.textContent = `${year - 1}年${monthNumber}月应累计预留 ${money(firstCents / 100)} 元，随后12个月每月约 ${money(monthly)} 元，到 ${year}年${monthNumber}月补齐预算。购房资金与普通策略国债相加持有，金额尾差自动补齐。`;
  };
  [enabled, month, amount].forEach((input) => input.addEventListener("input", update));
  update();
})();
