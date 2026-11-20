import {store} from '../../stores/research.js';
import {table} from './table.js';
import {status} from './status.js';
import {esc} from '../../utils/format.js';
export function runList(){return table(['编号 / 运行 / 版本','状态','源数据快照','核对与生效','复核'],store.runs.map(r=>[{html:`<a href="#results/${r.id}">#${r.id} v${r.version} ${esc(r.name)}</a>`},{html:status(r.status)},r.snapshot.length+'条 · '+r.checksum.slice(0,12),r.released?'已核对 · 当前生效':(r.review?'已复核 · 非当前版本':'待核对')+' '+(r.is_current?'· 指针当前':'') ,r.review?'已复核':'待负责人复核']));}
