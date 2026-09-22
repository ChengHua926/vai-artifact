from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
R=Path(__file__).resolve().parents[1];O=Path(__file__).resolve().parent
r=json.loads((R/'local-performance/runtime-summary.json').read_text());s=json.loads((R/'local-performance/scaling-summary.json').read_text())
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.labelcolor':'#26343b','text.color':'#26343b','axes.edgecolor':'#9aa6ac','xtick.color':'#52616b','ytick.color':'#52616b','savefig.facecolor':'white','pdf.fonttype':42})
fig,ax=plt.subplots(1,2,figsize=(10.2,3.8),gridspec_kw={'width_ratios':[1,1.15]})
colors=['#a7b3b9','#238b8d','#315e86'];keys=['capture_off','http_store','http_store_anvil']
means=[r[k]['total_action_plus_finalization_seconds']['mean'] for k in keys]
ax[0].bar(np.arange(3),means,color=colors,width=.57,zorder=3)
for i,v in enumerate(means):ax[0].text(i,v+.12,f'{v:.2f}s',ha='center',fontsize=11)
ax[0].set_xticks(range(3),['Capture\ndisabled','Capture +\nHTTP store','Capture + store\n+ local chain'])
ax[0].set_ylabel('Time for 100 native writes (seconds)');ax[0].set_ylim(0,9.3)
ax[0].set_title('(a) Runtime overhead',loc='left',fontweight='semibold',pad=14)
ax[0].grid(axis='y',color='#e4e9ec',zorder=0);ax[0].set_axisbelow(True)
xs=[100,1000,10000]
for cadence,color,label in [('every_10_records','#b06042','Checkpoint every 10 records'),('final_only','#238b8d','Final commitment only')]:
 ys=[s[f'{n}:{cadence}']['total_seconds']['mean'] for n in xs]
 ax[1].plot(xs,ys,marker='o',color=color,lw=2,ms=6,label=label)
 ax[1].annotate(f'{ys[-1]:.2f}s',(xs[-1],ys[-1]),xytext=(-7,10 if cadence=='every_10_records' else -17),textcoords='offset points',ha='right',color=color,fontsize=11)
ax[1].set_xscale('log');ax[1].set_yscale('log');ax[1].set_xticks(xs,['100','1,000','10,000'])
ax[1].set_yticks([.01,.1,1,10,100],['0.01','0.1','1','10','100']);ax[1].set_ylim(.007,110)
ax[1].set_ylabel('Off-chain verification (seconds, log scale)');ax[1].set_xlabel('Records per trace')
ax[1].set_title('(b) Cost of cumulative prefix checks',loc='left',fontweight='semibold',pad=14)
ax[1].grid(which='major',color='#e4e9ec');ax[1].legend(frameon=False,loc='upper left',fontsize=8.7)
fig.subplots_adjust(left=.075,right=.98,top=.86,bottom=.24,wspace=.34)
fig.text(.075,.035,'(a) 30 paired runs; real Hermes tools, controlled approval.  (b) 5 runs per point; chain RPC and settlement excluded.',fontsize=8,color='#60717b')
for ext in ['png','pdf']:fig.savefig(O/f'local-practicality.{ext}',dpi=180)
