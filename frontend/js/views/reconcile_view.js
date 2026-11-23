export const reconcileViewTemplate = `
<section class="panel">
  <h2>对账互证</h2>
  <p class="hint">当前代口径：可用 + 当前代已拨付 + 当前代已罚没 = 当前代代际基线（笼总额 − 旧代已结算）。旧代拨付/罚没只读留痕，绝不并入新代合计。</p>
  <p class="hint gen-guard">代际隔离（甲路径）：跨代键一律不计入当前代汇总，杜绝「旧拨付串进新代对账仍假绿」；当事人口径：账户冻结 = 活笼可用合计。</p>
  <div class="actions">
    <button @click="loadReconcile">刷新对账</button>
    <button class="secondary" @click="loadProof">当事人互证</button>
  </div>
  <div class="form-row" style="max-width:360px">
    <div>
      <label>当事人键（互证）</label>
      <input v-model="proofParty" list="party-list-recon"/>
      <datalist id="party-list-recon">
        <option v-for="b in bonds" :key="b.party_key" :value="b.party_key"/>
      </datalist>
    </div>
  </div>
  <div v-if="recon.summary" class="hint">
    合计 {{ recon.summary.total }} · 平衡 {{ recon.summary.balanced }} · 失衡 {{ recon.summary.unbalanced }}
  </div>
  <table>
    <thead>
      <tr>
        <th>笼</th><th>当前代</th><th>状态</th><th>笼总额</th><th>代际基线</th>
        <th>可用</th><th>当前代拨付</th><th>当前代罚没</th><th>旧代已结算(只读)</th>
        <th>合计</th><th>平衡</th><th>缺口</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="i in (recon.items||[])" :key="i.cage_id">
        <td>{{ i.cage_code }}</td>
        <td><strong>代{{ i.generation }}</strong></td>
        <td>{{ i.status }}</td>
        <td>{{ fmt(i.amount) }}</td>
        <td>{{ fmt(i.generation_baseline) }}</td>
        <td>{{ fmt(i.available) }}</td>
        <td>{{ fmt(i.released) }}</td>
        <td>{{ fmt(i.forfeited) }}</td>
        <td class="gen-old">{{ fmt((i.old_generation_released||0) + (i.old_generation_forfeited||0)) }}</td>
        <td>{{ fmt(i.accounted) }}</td>
        <td>
          <span class="badge" :class="i.balanced?'pass':'fail'">{{ i.balanced ? '是' : '否' }}</span>
        </td>
        <td>{{ fmt(i.gap) }}</td>
      </tr>
    </tbody>
  </table>
  <div v-if="partyProof" style="margin-top:14px">
    <h3>当事人互证 · {{ partyProof.party_key }}</h3>
    <div class="proof-box">{{ JSON.stringify(partyProof.proof, null, 2) }}
账户冻结 {{ fmt(partyProof.account.frozen) }}
活笼可用 {{ fmt(partyProof.live_cage_available) }}
匹配 {{ partyProof.frozen_matches_live }}
拨付合计 {{ fmt(partyProof.released_total) }}
罚没合计 {{ fmt(partyProof.forfeited_total) }}</div>
  </div>
</section>
`;

