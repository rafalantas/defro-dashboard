const $=id=>document.getElementById(id);
/* Animacja przepływu: jedna prędkość kropek, osobna (wolniejsza) prędkość kresek, kreski ciągłe na całej trasie. */
(function(){
  const BEAD_SPEED=110, BEAD_SPACING=300, DASH_PERIOD=18, svg=document.querySelector('.schematic');
  if(!svg) return;
  svg.querySelectorAll('g[id^="bead"]').forEach(g=>{
    const circles=[...g.querySelectorAll('circle.water-bead')];
    if(!circles.length) return;
    const mpath=circles[0].querySelector('mpath'), path=mpath&&svg.querySelector(mpath.getAttribute('href'));
    if(!path) return;
    const len=path.getTotalLength(), dur=len/BEAD_SPEED, radii=[4,3.5,3];
    const fixed=Number(g.dataset.beadSpacing)||0, want=fixed?Math.max(1,Math.round(len/fixed)):Math.max(circles.length,Math.round(len/BEAD_SPACING));
    while(circles.length<want){const c=circles[0].cloneNode(true);c.setAttribute('r',radii[circles.length%3]);g.append(c);circles.push(c);}
    circles.forEach((c,i)=>{const m=c.querySelector('animateMotion');m.setAttribute('dur',dur.toFixed(3)+'s');m.setAttribute('begin',(fixed?-(i*fixed/BEAD_SPEED):-dur*i/circles.length).toFixed(3)+'s');});
  });
  const fp=id=>svg.querySelector(`[data-fid="${id}"]`);
  const lenAt=(path,x,y)=>{let best=0,bd=Infinity;const total=path.getTotalLength();for(let s=0;s<=total;s+=1){const p=path.getPointAtLength(s),d=(p.x-x)**2+(p.y-y)**2;if(d<bd){bd=d;best=s;}}return best;};
  // [id, 'start'] | [id,'after',poprzedni,przerwa na urządzeniu] | [id,'tee',rodzic,x,y]
  const chain=[['sys','start'],['radUp','tee','sys',450,115],['floorUp','tee','sys',800,115],['dhwUp','tee','sys',1120,115],
    ['radDown','after','radUp',100],['floorDown','after','floorUp',100],['dhwDown','after','dhwUp',50],
    ['radRet','after','radDown',125],['floorRet','after','floorDown',135],['floorToCommon','tee','floorRet',1045,430],
    ['radRet2','after','radRet',80],['dhwTank','after','dhwDown',0],['dhwRet','after','dhwTank',0],
    ['commonRet','after','dhwRet',0],['sysRet','after','commonRet',0],['circHot','start'],['circRet','after','circHot',0]];
  const pos={};
  chain.forEach(([id,mode,ref,a,b])=>{
    const path=fp(id); if(!path) return;
    let start=0;
    if(mode==='after'&&fp(ref)) start=pos[ref]+fp(ref).getTotalLength()+a;
    else if(mode==='tee'&&fp(ref)) start=pos[ref]+lenAt(fp(ref),a,b);
    pos[id]=start;
    path.style.setProperty('--fp',(((start%DASH_PERIOD)+DASH_PERIOD)%DASH_PERIOD).toFixed(2)+'px');
  });
})();

