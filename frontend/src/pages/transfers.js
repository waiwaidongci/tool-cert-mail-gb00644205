import {feedback} from '../components/common/fields.js';
import {panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const STATE = {locked:'已锁定', conflict:'冲突'};

function releaseForm(item, refresh) {
  const message = feedback();
  const submit = button('释放占用', {type:'submit', class:'quiet'});
  return el('form', {class:'release-form', onSubmit:handler(submit, message, async event => {
    event.preventDefault();
    await workspace.releaseTransfer(item.id, new FormData(event.target).get('note'));
    await refresh();
  })}, el('input', {name:'note', required:true, maxlength:300, placeholder:'释放说明',
    'aria-label':`释放 ${item.spare_id} 的说明`}), submit, message);
}

function confirmForm(datasets, spec, refresh, error) {
  const submit = button('确认调拨', {type:'submit', class:'primary', 'data-testid':'confirm-transfer'});
  const prefix = `${spec.params.find(param => param.key === 'report_day')?.default || ''}`;
  return el('form', {class:'fields', 'data-testid':'transfer-form',
    onSubmit:handler(submit, error, async event => {
      event.preventDefault();
      const data = new FormData(event.target);
      await workspace.confirmTransfer({
        dataset_id:Number(data.get('dataset_id')), station_id:data.get('station_id'),
        work_area:data.get('work_area') || '', demand_asset_id:data.get('demand_asset_id'),
        spare_id:data.get('spare_id'), shift_start:data.get('shift_start'), shift_end:data.get('shift_end'),
        report_day:data.get('report_day') || null,
      });
      await refresh();
    })},
    el('label', {class:'field', for:'transfer-dataset'}, el('span', {}, '数据批次'),
      el('select', {id:'transfer-dataset', name:'dataset_id', required:true},
        datasets.map(item => el('option', {value:item.id}, `${item.name} · ${item.row_count} 行`)))),
    el('label', {class:'field', for:'transfer-station'}, el('span', {}, '工位编号'),
      el('input', {id:'transfer-station', name:'station_id', required:true, maxlength:40})),
    el('label', {class:'field', for:'transfer-area'}, el('span', {}, '作业区'),
      el('input', {id:'transfer-area', name:'work_area', maxlength:60})),
    el('label', {class:'field', for:'transfer-demand'}, el('span', {}, '需求器具编号'),
      el('input', {id:'transfer-demand', name:'demand_asset_id', required:true, maxlength:40})),
    el('label', {class:'field', for:'transfer-spare'}, el('span', {}, '备用具编号'),
      el('input', {id:'transfer-spare', name:'spare_id', required:true, maxlength:40})),
    el('label', {class:'field', for:'transfer-start'}, el('span', {}, '班次开始'),
      el('input', {id:'transfer-start', name:'shift_start', type:'datetime-local', required:true,
        value:`${prefix}T08:00`})),
    el('label', {class:'field', for:'transfer-end'}, el('span', {}, '班次结束'),
      el('input', {id:'transfer-end', name:'shift_end', type:'datetime-local', required:true,
        value:`${prefix}T16:00`})),
    el('label', {class:'field', for:'transfer-day'}, el('span', {}, '报告日'),
      el('input', {id:'transfer-day', name:'report_day', type:'date', value:prefix})),
    el('div', {class:'actions'}, submit), error);
}

export async function transfersPage(route, refresh) {
  const {spec, datasets} = workspace;
  const items = await workspace.transfers();
  const error = feedback();
  const locked = items.filter(item => item.state === 'locked');
  const conflicts = items.filter(item => item.state === 'conflict');
  const released = items.filter(item => item.released_at);
  const action = row => session.canReview ? releaseForm(row, refresh) : '—';
  const confirm = session.canReview && datasets.length
    ? confirmForm(datasets, spec, refresh, error)
    : p(datasets.length ? '只读角色可查看调拨状态。' : '请先导入数据批次。', 'empty');

  const lockTable = table([
    {label:'工位', render:row=>`${row.station_id} · ${row.work_area}`},
    {key:'demand_asset_id', label:'需求器具'},
    {key:'spare_id', label:'备用具'},
    {label:'班次', render:row=>`${row.shift_start} 至 ${row.shift_end}`},
    {label:'状态', render:row=>pill(STATE[row.state] || row.state, row.state)},
    {label:'操作', render:action},
  ], locked, {caption:'已锁定调拨', empty:'还没有已确认的调拨。'});
  const conflictTable = table([
    {label:'工位', render:row=>`${row.station_id} · ${row.work_area}`},
    {key:'demand_asset_id', label:'需求器具'},
    {key:'spare_id', label:'备用具'},
    {label:'班次', render:row=>`${row.shift_start} 至 ${row.shift_end}`},
    {key:'reason', label:'冲突原因'},
    {label:'处置', render:row=>session.canReview ? releaseForm(row, refresh) : '—'},
  ], conflicts, {caption:'冲突调拨', empty:'没有冲突的调拨。'});
  const releasedTable = table([
    {label:'工位', render:row=>`${row.station_id} · ${row.work_area}`},
    {key:'spare_id', label:'备用具'},
    {key:'release_note', label:'释放说明'},
    {label:'释放时间', render:row=>date(row.released_at)},
  ], released.slice(0, 30), {caption:'已释放调拨', empty:'还没有已释放的调拨。'});

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel(spec.pages[6],
        p('确认已落实的备用具调拨后即锁定该器具；规划会优先保留锁定。源数据更新使其失效时标记冲突并继续占用，释放后再重排。', 'subtle'),
        confirm),
      panel('已锁定调拨', lockTable),
      panel('冲突调拨', conflictTable),
      panel('已释放调拨', releasedTable)),
    el('aside', {class:'sidebar-panel'},
      panel('锁定与冲突口径', el('ul', {class:'method-list'}, [
        '同一把备用具可复用到不同班次，但须留出上一班次归还后到下一工位的路程。',
        '量程须覆盖需求器具；证书须覆盖班次使用全程。',
        '冲突调拨保留占用，不自动改派，避免把同一把排给赶不上的两站。',
      ].map(text=>el('li', {}, text)))),
      panel('相关入口', link('查看班报去向', '#results', {class:'quiet'}),
        link('重导入源数据', '#inputs', {class:'quiet'}))));
}
