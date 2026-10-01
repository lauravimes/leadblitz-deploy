(function(){
  const form=document.getElementById('plan-form'); if(!form)return;
  const steps=Array.from(form.querySelectorAll('.plan-step'));
  const back=document.getElementById('plan-back'),next=document.getElementById('plan-next'),submit=document.getElementById('plan-submit');
  const label=document.getElementById('step-label'),progress=document.getElementById('plan-progress');
  let current=0,started=false;
  function show(focus){steps.forEach((s,i)=>{s.hidden=i!==current;s.querySelectorAll('input').forEach(el=>el.required=i===current);});back.hidden=current===0;next.hidden=current===steps.length-1;submit.hidden=current!==steps.length-1;label.hidden=false;progress.hidden=false;label.textContent='QUESTION '+(current+1)+' OF '+steps.length;progress.value=current+1;if(focus){const el=steps[current].querySelector('input');if(el)el.focus();}}
  function start(){if(started)return;started=true;fetch('/client-plan/start',{method:'POST',body:new URLSearchParams({csrf:form.elements.csrf.value}),credentials:'same-origin'}).catch(()=>{});}
  function valid(){return Array.from(steps[current].querySelectorAll('input')).every(el=>el.reportValidity());}
  form.addEventListener('change',start);form.addEventListener('input',start);
  next.addEventListener('click',()=>{if(valid()){current++;show(true);}});back.addEventListener('click',()=>{current--;show(true);});
  form.addEventListener('submit',e=>{if(current<steps.length-1){e.preventDefault();if(valid()){current++;show(true);}}else{const empty=steps.findIndex(s=>{const radio=s.querySelector('input[type=radio]');return radio?!s.querySelector('input:checked'):!s.querySelector('input').value.trim();});if(empty>=0){e.preventDefault();current=empty;show(true);valid();}}});
  show(false);
})();
