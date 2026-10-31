globalThis.ProxHASelection = {
  update(selected, anchor, id, ordered, {shiftKey = false, ctrlKey = false, metaKey = false} = {}) {
    const additive = ctrlKey || metaKey;
    if (shiftKey && ordered.includes(anchor) && ordered.includes(id)) {
      const a = ordered.indexOf(anchor), b = ordered.indexOf(id);
      const range = ordered.slice(Math.min(a, b), Math.max(a, b) + 1);
      return {selected: new Set(additive ? [...selected, ...range] : range), anchor};
    }
    const next = new Set(additive ? selected : []);
    if (additive && next.has(id)) next.delete(id); else next.add(id);
    return {selected: next, anchor: id};
  },
  unavailable(action, guests, nodes) {
    if (!guests.length) return 'Select at least one guest.';
    if (guests.some(g => g.template)) return 'Templates are not supported.';
    if (guests.some(g => !nodes.some(n => n.node === g.node && n.status === 'online'))) return 'A source host is offline.';
    if (guests.some(g => g.lock)) return 'A guest is locked by another operation.';
    if (['reset', 'hibernate', 'free-balloon'].includes(action) && guests.some(g => g.type !== 'qemu')) return 'Only available for QEMU VMs.';
    if (action === 'start' && guests.some(g => g.status !== 'stopped')) return 'All selected guests must be stopped.';
    if (!['start', 'migrate'].includes(action) && guests.some(g => g.status !== 'running')) return 'All selected guests must be running.';
    if (['shutdown', 'reboot', 'hibernate', 'free-balloon'].includes(action) && guests.some(g => ['paused', 'suspended'].includes(g.qmpstatus))) return 'Resume paused guests in Proxmox first.';
    if (action === 'free-balloon') {
      if (guests.length !== 1) return 'Select one VM at a time for Free balloon.';
      const g = guests[0];
      if (g.qmpstatus && g.qmpstatus !== 'running') return 'Free balloon requires a running VM.';
      if (!Number.isFinite(g.balloon) || g.balloon <= 0 || !Number.isFinite(g.maxmem) || g.maxmem <= 0 || g.balloon > g.maxmem) return 'Live balloon allocation is unavailable. Refresh and check the VM balloon driver.';
      if (g.balloon === g.maxmem) return 'Full RAM is already available to this VM.';
    }
    if (action === 'hibernate' && guests.some(g => g.ha?.managed)) return 'HA-managed VMs cannot hibernate.';
    if (action === 'migrate' && !nodes.some(n => n.status === 'online' && guests.every(g => g.node !== n.node))) return 'No common target host is available.';
    return '';
  },
  summary(resources, key) {
    const known = resources.filter(r => Number.isFinite(r[key]));
    return {value: known.length ? known.reduce((sum, r) => sum + r[key], 0) : null, known: known.length, total: resources.length};
  }
};
