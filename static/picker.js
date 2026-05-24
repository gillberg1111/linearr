// Show picker: handles a "selected tray" + "available grid" with filter + pagination.
//
// Selected tiles physically move from the grid into the tray and back.
// The form's `name="shows"` submission order = the order tiles appear in the tray,
// which matches the user's selection order (or the pre-checked order on load).
//
// Usage from a template:
//   <script src="{{ url_for('static', filename='picker.js') }}"></script>
//   <script>setupPicker({ ... });</script>

(function () {
  function $(sel, root) { return (root || document).querySelector(sel); }

  function setupPicker(opts) {
    const grid = opts.grid;
    const tray = opts.tray;
    if (!grid || !tray) return;

    const allTiles = Array.from(grid.querySelectorAll('.show-tile'));

    let filter = '';
    let page = 0;
    // When no pagination UI is provided, show everything in one page.
    let pageSize = (() => {
      if (!opts.pageSizeSelect) return 'all';
      const v = opts.pageSizeSelect.value;
      return v === 'all' ? 'all' : (parseInt(v, 10) || 50);
    })();

    function update() {
      const filterStr = filter.toLowerCase();
      const selected = [];
      const filteredAvailable = [];

      // Categorize every tile
      for (const tile of allTiles) {
        const cb = tile.querySelector('input[type=checkbox]');
        if (!cb) continue;
        if (cb.checked) {
          selected.push(tile);
        } else if (!filterStr || (tile.dataset.label || '').includes(filterStr)) {
          filteredAvailable.push(tile);
        }
      }

      // Move selected to tray (preserving selection order)
      for (const tile of selected) {
        if (tile.parentElement !== tray) tray.appendChild(tile);
        tile.style.display = '';
      }

      // Paginate filtered-available
      const total = filteredAvailable.length;
      const totalPages = pageSize === 'all' ? 1 : Math.max(1, Math.ceil(total / pageSize));
      if (page > totalPages - 1) page = Math.max(0, totalPages - 1);
      const start = pageSize === 'all' ? 0 : page * pageSize;
      const end = pageSize === 'all' ? total : start + pageSize;

      // Hide everything not in tray, then re-show the current page
      for (const tile of allTiles) {
        const cb = tile.querySelector('input[type=checkbox]');
        if (cb && cb.checked) continue;
        if (tile.parentElement !== grid) grid.appendChild(tile);
        tile.style.display = 'none';
      }
      for (let i = start; i < end && i < total; i++) {
        filteredAvailable[i].style.display = '';
      }

      // Tray visibility / counter / pagination labels
      if (opts.trayWrapper) opts.trayWrapper.hidden = selected.length === 0;
      if (opts.counter) opts.counter.textContent = selected.length;
      if (opts.trayCount) opts.trayCount.textContent = selected.length;
      if (opts.pageInfo) {
        if (pageSize === 'all') {
          opts.pageInfo.textContent = `${total} available`;
        } else {
          opts.pageInfo.textContent =
            `Page ${page + 1} of ${totalPages} · ${total} available`;
        }
      }
      if (opts.prevBtn) opts.prevBtn.disabled = page === 0;
      if (opts.nextBtn) opts.nextBtn.disabled = pageSize === 'all' || page >= totalPages - 1;
      if (opts.emptyMsg) {
        opts.emptyMsg.hidden = total > 0 || allTiles.length === 0;
      }
    }

    // Event wiring
    if (opts.filterInput) {
      opts.filterInput.addEventListener('input', () => {
        filter = opts.filterInput.value;
        page = 0;
        update();
      });
    }
    if (opts.pageSizeSelect) {
      opts.pageSizeSelect.addEventListener('change', () => {
        const v = opts.pageSizeSelect.value;
        pageSize = v === 'all' ? 'all' : (parseInt(v, 10) || 50);
        page = 0;
        update();
      });
    }
    if (opts.prevBtn) {
      opts.prevBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (page > 0) { page--; update(); }
      });
    }
    if (opts.nextBtn) {
      opts.nextBtn.addEventListener('click', (e) => {
        e.preventDefault();
        page++;
        update();
      });
    }
    if (opts.clearBtn) {
      opts.clearBtn.addEventListener('click', (e) => {
        e.preventDefault();
        for (const tile of allTiles) {
          const cb = tile.querySelector('input[type=checkbox]');
          if (cb) cb.checked = false;
        }
        update();
      });
    }
    // Listen for ANY checkbox change inside our managed tiles
    const root = opts.formRoot || grid.closest('form') || document;
    root.addEventListener('change', (e) => {
      if (e.target && e.target.matches && e.target.matches('input[type=checkbox][name="shows"]')) {
        update();
      }
    });

    // First render — picks up any server-pre-checked items
    update();
  }

  window.setupPicker = setupPicker;
})();
