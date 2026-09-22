from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

O=Path(__file__).resolve().parent
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,
    'axes.spines.right':False,'axes.spines.left':False,'pdf.fonttype':42})
fig,ax=plt.subplots(figsize=(7.8,3.8))
totals=np.array([266,161]);parts=[np.array([125,70]),np.array([35,30]),np.array([106,61])]
bottom=np.zeros(2)
for values,color,label,hatch in zip(parts,['#245d81','#b7d5df','#d9dddf'],
    ['Credited destination violations','Other alarms, not credited','Successful attack, no alarm'],[None,'////',None]):
    heights=values/totals*100
    ax.bar(np.arange(2),heights,bottom=bottom,color=color,width=.45,label=label,hatch=hatch,
        edgecolor='#ffffff' if hatch is None else '#7196a7',linewidth=.8)
    for i in range(2):ax.text(i,bottom[i]+heights[i]/2,f'{values[i]}\n({heights[i]:.1f}%)',
        ha='center',va='center',color='white' if color=='#245d81' else '#253a44',fontsize=10)
    bottom+=heights
ax.set_xticks([0,1],['GLM\n266 successful attacks','Qwen\n161 successful attacks'])
ax.set_ylim(0,100);ax.set_yticks([0,25,50,75,100]);ax.set_ylabel('Successful attacker objectives (%)')
ax.set_axisbelow(True);ax.grid(axis='y',color='#eceff1');ax.tick_params(axis='both',length=0)
ax.set_title('AgentDojo: separate an alarm from a credited detection',loc='left',fontsize=12,pad=15)
ax.legend(frameon=False,loc='upper left',bbox_to_anchor=(1.03,1.0),fontsize=9)
fig.subplots_adjust(left=.1,right=.64,bottom=.24,top=.84)
fig.text(.1,.035,'All 125/125 GLM and 70/70 Qwen eligible destination violations were detected.\nHatched alarms check a different or additional issue; they do not extend the frozen coverage result.',fontsize=8,color='#536773')
for ext in ['png','pdf']:fig.savefig(O/f'agentdojo-alarm-distinction.{ext}',dpi=180)
