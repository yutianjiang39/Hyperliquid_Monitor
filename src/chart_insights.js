/* Statistics for the visible curve, without changing its accounting values.
 * Quantiles count each supplied valid point once; they are not time-weighted.
 * Missing values split independent drawdown histories. A drawdown ends only
 * when its previous peak is reached again, or when the data/window ends.
 */
(function (root) {
  'use strict';

  function finite(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function elapsed(start, end) {
    if (!finite(start.time) || !finite(end.time) || end.time < start.time) {
      return null;
    }
    return end.time - start.time;
  }

  function quantile(sorted, fraction) {
    if (!sorted.length) return null;
    const position = (sorted.length - 1) * fraction;
    const lower = Math.floor(position);
    const remainder = position - lower;
    if (!remainder) return sorted[lower];
    // Weighted terms avoid overflow in (upper - lower) for large opposites.
    return sorted[lower] * (1 - remainder) + sorted[lower + 1] * remainder;
  }

  /**
   * Analyze ordered {value, time, rank, index} records in the visible window.
   * Values must be finite numbers: null, NaN, strings and infinities are gaps.
   * Returned points are shallow copies; a missing index uses the input offset.
   *
   * Independent drawdowns share at most a recovery/peak endpoint. Nested dips
   * before recovery belong to one episode, whose lowest trough determines the
   * amount. Equal peaks before a decline use the latest point; equal troughs
   * use the first point. Exact >= comparisons preserve genuinely small dips
   * and do not silently round an unrecovered episode into a recovered one.
   *
   * Status: recovered = old peak reached; open = window ended underwater;
   * gap = missing data interrupted the episode, recovery remains unknown.
   * declineMs = peak to trough; recoveryMs = trough to recovered point;
   * underwaterMs = peak to end. Invalid/reversed timestamps yield null.
   *
   * Optional capitalOf(source, inputOffset) supplies funds at the peak event.
   * It receives that original source record and its offset in the input array,
   * not the record's optional index. No balance is inferred from curve values.
   * peakCapital preserves a finite supplied balance (including zero/negative);
   * percentage = amount / peakCapital * 100 only for positive known capital.
   * Unknown/non-positive capital or a non-finite result yields percentage=null.
   */
  function analyze(points, options) {
    const input = Array.isArray(points) ? points : [];
    const requestedLimit = options && options.limit;
    const limit = finite(requestedLimit)
      ? Math.max(0, Math.floor(requestedLimit)) : 3;
    const capitalOf = options && typeof options.capitalOf === 'function'
      ? options.capitalOf : null;
    const result = {
      count: 0,
      min: null,
      max: null,
      median: null,
      q1: null,
      q3: null,
      mean: null,
      first: null,
      last: null,
      drawdowns: [],
      drawdownCount: 0,
      segmentCount: 0
    };
    const values = [];
    const episodes = [];
    let peak = null;
    let trough = null;
    let previous = null;
    let peakOrder = 0;
    let mean = 0;

    function finish(status, end) {
      if (!peak || !trough || trough.value >= peak.value) return;
      const recovery = status === 'recovered' ? end : null;
      const amount = peak.value - trough.value;
      const suppliedCapital = capitalOf ? capitalOf(input[peakOrder], peakOrder) : null;
      const peakCapital = finite(suppliedCapital) ? suppliedCapital : null;
      const percentage = peakCapital !== null && peakCapital > 0
        ? amount / peakCapital * 100 : null;
      episodes.push({
        peak: peak,
        trough: trough,
        recovery: recovery,
        end: end,
        amount: amount,
        peakCapital: peakCapital,
        percentage: finite(percentage) ? percentage : null,
        status: status,
        declineMs: elapsed(peak, trough),
        recoveryMs: recovery ? elapsed(trough, recovery) : null,
        underwaterMs: elapsed(peak, end),
        order: peakOrder
      });
    }

    input.forEach(function (source, index) {
      if (!source || !finite(source.value)) {
        finish('gap', previous);
        peak = trough = previous = null;
        return;
      }
      const point = Object.assign({}, source, {
        index: finite(source.index) ? source.index : index
      });
      values.push(point.value);
      result.count += 1;
      if (result.first === null) result.first = point;
      result.last = point;
      if (result.min === null || point.value < result.min.value) result.min = point;
      if (result.max === null || point.value > result.max.value) result.max = point;
      mean = mean * ((result.count - 1) / result.count) + point.value / result.count;

      if (peak === null) {
        result.segmentCount += 1;
        peak = trough = point;
        peakOrder = index;
      } else if (point.value >= peak.value) {
        finish('recovered', point);
        peak = trough = point;
        peakOrder = index;
      } else if (point.value < trough.value) {
        trough = point;
      }
      previous = point;
    });
    finish('open', previous);

    values.sort(function (a, b) { return a - b; });
    result.median = quantile(values, 0.5);
    result.q1 = quantile(values, 0.25);
    result.q3 = quantile(values, 0.75);
    result.mean = result.count ? mean : null;
    result.drawdownCount = episodes.length;
    episodes.sort(function (a, b) {
      return b.amount - a.amount || a.order - b.order;
    });
    result.drawdowns = episodes.slice(0, limit).map(function (episode) {
      // Keep the tie-breaker's input offset internal, not part of the API.
      const copy = Object.assign({}, episode);
      delete copy.order;
      return copy;
    });
    return result;
  }

  const api = Object.freeze({analyze: analyze});
  root.HLChartInsights = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
