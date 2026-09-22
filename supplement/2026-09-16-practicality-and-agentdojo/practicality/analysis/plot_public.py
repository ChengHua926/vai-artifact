from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

O=Path(__file__).resolve().parent
x=json.loads((O/'public-summary.json').read_text())
keys=['10_records_or_30s','30_records_or_60s','final_only']
labels=['10 records\nor 30 seconds','30 records\nor 60 seconds','Final commitment\nonly']
colors=['#245d81','#388f94','#9aabb5']
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,
    'axes.spines.right':False,'axes.labelcolor':'#26343b','text.color':'#26343b',
    'axes.edgecolor':'#9aa6ac','xtick.color':'#52616b','ytick.color':'#52616b',
    'savefig.facecolor':'white','pdf.fonttype':42})
fig,axes=plt.subplots(1,2,figsize=(9.6,3.9))
for ax,metric,scale,title,ylabel in [
    (axes[0],'total_fee_wei',1e12,'(a) Checkpoint and final commitment fees','Fee per session (µETH)'),
    (axes[1],'max_unanchored_age_seconds',1,'(b) Longest observed time unanchored','Seconds')]:
    for i,k in enumerate(keys):
        vals=[r[metric]/scale for r in x['checkpoint_rows'] if r['strategy']==k]
        mean=np.mean(vals)
        ax.bar(i,mean,width=.55,color=colors[i],zorder=2)
        ax.plot([i,i],[min(vals),max(vals)],color='#26343b',lw=1,zorder=3)
        ax.scatter([i-.065,i,i+.065],vals,color='#26343b',s=14,zorder=4)
        ax.text(i,max(vals)*1.03+.04,f'{mean:.2f}' if scale>1 else f'{mean:.1f}',ha='center',fontsize=11)
    ax.set_xticks(range(3),labels)
    ax.set_ylabel(ylabel)
    ax.set_title(title,loc='left',fontweight='semibold',pad=13,fontsize=11)
    ax.set_ylim(bottom=0,top=ax.get_ylim()[1]*1.12)
    ax.grid(axis='y',color='#e4e9ec',zorder=0)
    ax.set_axisbelow(True)
fig.subplots_adjust(left=.08,right=.99,top=.85,bottom=.28,wspace=.29)
fig.text(.08,.045,'Base mainnet · 20 native writes / 80 records per session · 3 repetitions per policy (dots)\nTiming includes paced public RPC and retries; measured through receipt observation, not L1 finality.',fontsize=8,color='#60717b')
for ext in ['png','pdf']:fig.savefig(O/f'public-checkpoints.{ext}',dpi=180)
