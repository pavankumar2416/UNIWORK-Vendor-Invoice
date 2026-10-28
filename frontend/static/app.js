document.addEventListener('DOMContentLoaded', () => {
  // A link opened in a new tab must start a fresh tab-specific login session.
  // Same-tab navigation keeps the __tab token appended by Flask.
  document.querySelectorAll('a[target="_blank"]').forEach(link => {
    try {
      const targetUrl = new URL(link.href, window.location.href);
      targetUrl.searchParams.delete('__tab');
      link.href = targetUrl.toString();
    } catch (_) {}
  });
  const toggle = document.getElementById('menuToggle');
  const sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) toggle.addEventListener('click', () => sidebar.classList.toggle('sidebar-open'));


  const globalSearch = document.getElementById('globalSearch');
  if (globalSearch) {
    globalSearch.addEventListener('input', () => {
      const query = globalSearch.value.trim().toLowerCase();
      document.querySelectorAll('table tbody tr').forEach(row => {
        row.hidden = query !== '' && !row.innerText.toLowerCase().includes(query);
      });
      document.querySelectorAll('.vendor-card, .metric-card, .report-card, .activity-item').forEach(card => {
        card.hidden = query !== '' && !card.innerText.toLowerCase().includes(query);
      });
    });
    globalSearch.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        globalSearch.value = '';
        globalSearch.dispatchEvent(new Event('input'));
      }
    });
  }

  document.querySelectorAll('[data-table-search]').forEach(input => {
    input.addEventListener('input', () => {
      const table = document.getElementById(input.dataset.tableSearch);
      if (!table) return;
      const query = input.value.trim().toLowerCase();
      table.querySelectorAll('tbody tr').forEach(row => {
        row.hidden = !row.innerText.toLowerCase().includes(query);
      });
    });
  });

  document.querySelectorAll('.flash').forEach(el => {
    window.setTimeout(() => {
      if (el.isConnected) {
        el.style.opacity = '0';
        el.style.transform = 'translateY(-4px)';
        window.setTimeout(() => el.remove(), 250);
      }
    }, 6500);
  });

  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', e => {
      if (!form.checkValidity()) {
        e.preventDefault();
        form.reportValidity();
      }
    });
  });
});