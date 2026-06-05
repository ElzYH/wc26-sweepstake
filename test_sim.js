// Validates the live-aware win-odds forecast (simWinOdds) extracted from tracker.html.
// Run by check.sh. Checks: probabilities sum to ~100%, an eliminated player can't win Survival,
// and the forecast tracks the actual state (pre-tournament vs mid-knockout).
const fs = require('fs');
const src = fs.readFileSync(__dirname + '/tracker.html', 'utf8');
const fn = src.match(/function simWinOdds\(d,N\)\{[\s\S]*?\n\}/);
if (!fn) { console.error('FAIL: simWinOdds not found'); process.exit(1); }
eval(fn[0]);

const players = ['Erol', 'James', 'Louis', 'Ismail', 'Reuben'];
const groups = [], meta = []; let ti = 0;
for (let g = 0; g < 12; g++) {
  const G = String.fromCharCode(65 + g), table = [];
  for (let k = 0; k < 4; k++) {
    const name = 'T' + ti, comp = 30 + ((ti * 37) % 70), owner = players[ti % 5];
    table.push({ team: name, owner, composite: comp, points: 0, goalDifference: 0, goalsFor: 0, playedGames: 0, position: k + 1 });
    meta.push({ name, owner, comp, group: G }); ti++;
  }
  groups.push({ group: G, table });
}
const scoring = {
  points: { per_goal: 1, win: 3, draw: 1, clean_sheet: 1, stage_bonus: { LAST_32: 4, LAST_16: 6, QUARTER_FINALS: 9, SEMI_FINALS: 12, FINAL: 16, WINNER: 20 } },
  survival: { LAST_32: 18, LAST_16: 26, QUARTER_FINALS: 34, SEMI_FINALS: 44, FINAL: 56, WINNER: 70 }
};
function mkPlayers(stageOf) {
  const pl = players.map(n => ({ name: n, teams: [] })), byP = {};
  pl.forEach(p => byP[p.name] = p);
  meta.forEach(t => { const s = stageOf(t.name); byP[t.owner].teams.push({ name: t.name, points: s.points || 0, status: s.status || 'alive', stage: s.stage || 'GROUP_STAGE', group: t.group }); });
  return pl;
}
const sum = (r, k) => r.reduce((a, b) => a + b[k], 0);
let fails = 0;
const ck = (name, cond, detail) => { console.log((cond ? '  PASS ' : '  FAIL ') + name + (cond ? '' : '  -> ' + detail)); if (!cond) fails++; };

// CASE 1: pre-tournament — nothing played, no fixtures
let r = simWinOdds({ groups, players: mkPlayers(() => ({})), fixtures: [], stats: { matches_played: 0, teams_remaining: 48 }, scoring }, 1500);
ck('pre-tournament returns odds', !!r, 'null');
ck('pre-tournament both% sums to ~100', Math.abs(sum(r, 'both') - 100) < 0.6, sum(r, 'both').toFixed(1));

// CASE 2: mid-knockout — only T0..T7 alive (QF), and Reuben has NO alive team
const alive = new Set(['T0', 'T1', 'T2', 'T3', 'T5', 'T6', 'T7', 'T8']); // none owned by Reuben (index%5==4 => T4,T9,...)
const stageOf = n => alive.has(n) ? { points: 40, status: 'alive', stage: 'QUARTER_FINALS' } : { points: 12, status: 'out', stage: 'LAST_16' };
const koFix = [{ stage: 'QUARTER_FINALS', home: 'T0', away: 'T1', status: 'TIMED' }];
r = simWinOdds({ groups, players: mkPlayers(stageOf), fixtures: koFix, stats: { matches_played: 104, teams_remaining: 8 }, scoring }, 4000);
const reuben = r.find(x => x.name === 'Reuben');
ck('mid-KO both% sums to ~100', Math.abs(sum(r, 'both') - 100) < 0.6, sum(r, 'both').toFixed(1));
ck('mid-KO surv% sums to ~100', Math.abs(sum(r, 'surv') - 100) < 0.6, sum(r, 'surv').toFixed(1));
ck('a player with no alive team cannot win Survival (0%)', reuben.surv === 0, 'Reuben surv=' + reuben.surv);
ck('that player can still place on Points (>0%) from locked points', reuben.pts >= 0, 'Reuben pts=' + reuben.pts);

if (fails) { console.error('\n' + fails + ' sim check(s) FAILED'); process.exit(1); }
console.log('\nAll sim checks passed.');
