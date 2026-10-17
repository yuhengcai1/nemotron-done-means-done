import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

const workspaceDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rootDir = path.resolve(workspaceDir, '../..');
const SKILL_DIR = '/Users/caiyuheng/.codex/plugins/cache/openai-primary-runtime/presentations/26.1011.51657/skills/presentations';
const RUNTIME_PYTHON = '/Users/caiyuheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
process.env.RUNTIME_NODE_MODULES ??= '/Users/caiyuheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const { resolvePresentationFont, finalizePresentation } = await import(pathToFileURL(path.join(SKILL_DIR, 'container_tools/artifact_tool_utils.mjs')).href);
const font = resolvePresentationFont();
function option(name,fallback) {
  const index=process.argv.indexOf(name);
  if (index<0) return fallback;
  if (!process.argv[index+1] || process.argv[index+1].startsWith('--')) throw new Error(`Missing ${name} value`);
  return path.resolve(process.argv[index+1]);
}
const factsPath = option('--facts',path.join(workspaceDir, 'facts.json'));
const previewsDir=option('--previews',path.join(workspaceDir,'previews'));
const facts = JSON.parse(await fs.readFile(factsPath, 'utf8'));
const draft = process.argv.includes('--draft');
if (!draft && (!facts.complete || !facts.screenshot || !facts.result_source)) {
  throw new Error('Final build requires a completed live-run artifact, result source and actual UI screenshot.');
}
if (!draft && !['opening','problem','demo','results','close'].every(key=>typeof facts.script?.[key]==='string')) {
  throw new Error('Final speaker script requires all five narrative segments.');
}
if (!draft && facts.verified) {
  if (!facts.verified_inputs?.sha256) throw new Error('Successful-repair deck requires evidence-bound facts.');
  for (const [relative,digest] of Object.entries(facts.verified_inputs.sha256)) {
    const actual=createHash('sha256').update(await fs.readFile(path.join(rootDir,relative))).digest('hex');
    if (actual!==digest) throw new Error(`Evidence changed after facts preparation: ${relative}`);
  }
}
const timing={opening:'00:00–00:20',problem:'00:20–00:45',demo:'00:45–02:15',results:'02:15–02:40',close:'02:40–03:00',...facts.timing};
const palette = { cream:'#F7F5EE', ink:'#14251F', forest:'#234B3B', orange:'#D66C43', muted:'#647269' };
const p = Presentation.create({ slideSize: { width:1280, height:720 } });

function text(slide, name, content, x, y, w, h, size, color=palette.ink, bold=false, extra={}) {
  const s = slide.shapes.add({ geometry:'textbox', name, position:{left:x,top:y,width:w,height:h}, fill:'none', line:{fill:'none',width:0} });
  s.text = content;
  s.text.style = { typeface:font, fontSize:size, bold, color, insets:0, wrap:'square', autoFit:'none', ...extra };
  return s;
}
function slide(bg=palette.cream) { const s=p.slides.add(); s.background.fill=bg; return s; }
function notes(s, index, copy, sources=[]) {
  s.speakerNotes.text = `RECURSIVE LAB — 3-MINUTE DEMO\n${index}\n\n${copy}\n\nSOURCES\n${sources.map(x=>path.isAbsolute(x)?x:path.join(rootDir,x)).join('\n')}`;
}

// 1 — Minimal editable title.
{
  const s=slide(palette.forest);
  text(s,'product-title','Recursive Lab',72,142,1136,132,106,palette.cream,true);
  text(s,'product-description',facts.cover_subtitle || 'Autonomous debugging\nwith Nemotron',78,313,1070,134,54,palette.cream);
  text(s,'sponsor-context',facts.cover_provider || 'Crusoe Managed Inference',80,600,1040,40,28,'#D7DFD6');
  notes(s,timing.opening,facts.script?.opening || 'An agent can finish a task and still leave a broken framework behind. Recursive Lab gives Nemotron a second job: inspect the framework that runs it, reproduce a defect, propose a repair, and keep only the fixes that survive verification.',['examples/self_debug.py']);
}

// 2 — Actual defect, explained through type rather than an invented interface.
{
  const s=slide();
  text(s,'failure-title','When the framework misclassifies a failure',72,60,1136,72,48,palette.ink,true);
  text(s,'first-observation-label','OBSERVATION',76,202,1060,36,22,palette.muted,true);
  text(s,'first-observation','The evaluator recorded a failure.',72,248,1136,76,48,palette.forest,true);
  text(s,'later-error-label','FOLLOWING TOOL CALL',76,376,1060,36,22,palette.muted,true);
  text(s,'later-error','A later tool error overwrote its status.',72,422,1136,84,46,palette.orange,true);
  text(s,'repair-principle','Historical failing revision of our local framework',76,589,1100,56,29,palette.ink);
  notes(s,timing.problem,facts.script?.problem || 'Here is a real defect from our framework. The evaluator recorded a failure. A later tool call failed, and the finalizer replaced the violation status with an error despite retaining the evidence. We give Nemotron the source and a failing test, then let it work through the repair.',[facts.target_source || 'integrations/teacher_student/root/frozen_targets.py', facts.result_source || 'examples/self_debug.py']);
}

// 3 — Real product screenshot. During the talk, switch to the local app.
{
  const s=slide();
  text(s,'demo-title',facts.verified?'Nemotron runs the debugging loop':'Nemotron attempts a framework repair',72,51,1136,69,46,palette.ink,true);
  if (facts.screenshot) {
    const imagePath=path.isAbsolute(facts.screenshot)?facts.screenshot:path.join(rootDir,facts.screenshot);
    const b=await fs.readFile(imagePath);
    s.images.add({blob:b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength),contentType:/\.jpe?g$/i.test(imagePath)?'image/jpeg':'image/png',alt:'Actual Recursive Lab frontend showing the recorded Nemotron self-debugging run',fit:'contain',position:{left:72,top:145,width:1136,height:495}});
  } else {
    text(s,'demo-placeholder','Actual run preview pending',72,265,1136,90,50,palette.muted);
  }
  text(s,'demo-caption',facts.replay_caption || 'Recorded model actions, executable tests and retained evidence',76,654,1120,34,22,palette.muted);
  notes(s,timing.demo+' — LIVE APP',facts.script?.demo || 'Switch to the local app. Show the goal and source. Open the model plan. Open the failing test. Open the patch. Show independent verification. Open the stored memory. Return to slides at 02:15. Explain any failed attempt plainly and distinguish recorded replay from a new model call.',[facts.result_source || 'examples/self_debug.py',facts.screenshot || 'demo/web/index.html']);
}

// 4 — Only measured outcomes from the actual runtime artifact.
{
  const s=slide();
  text(s,'result-title',facts.result_title || (facts.verified?'A repair with executable evidence':(facts.complete?'Checks rejected the proposed repair':'Evidence pending')),72,60,1136,70,48,palette.ink,true);
  const metrics=facts.metrics || [
    {value:'—',label:'model calls'}, {value:'—',label:'verification checks'}, {value:'—',label:'retained repair'}
  ];
  metrics.forEach((m,i)=>{
    const x=76+i*388;
    text(s,`metric-${i}-value`,m.value,x,223,340,135,104,i===2?palette.orange:palette.forest,true);
    text(s,`metric-${i}-label`,m.label,x,376,338,80,27,palette.ink);
  });
  text(s,'result-meaning',facts.outcome || 'Results will come from the captured self-debugging run.',76,507,1104,90,32,palette.ink,true);
  text(s,'result-scope',facts.scope || 'Bounded local framework repair. Initial framework code authored with Codex.',76,644,1120,36,21,palette.muted);
  notes(s,timing.results,(facts.script?.results || 'The result is a patch with executable evidence. Report only the counts visible in the run artifact. The model proposed the repair, while separate test execution determined whether it worked. This is a bounded local repair of our framework. The initial framework was authored with Codex.') + (facts.provenance_notes ? '\n\nPROVENANCE\n'+facts.provenance_notes : ''),[facts.result_source || 'examples/self_debug.py',...(facts.additional_sources||[])]);
}

// 5 — Plain close anchored to actual runtime behavior.
{
  const s=slide(palette.forest);
  if (facts.roadmap) {
    text(s,'close-title','Roadmap',72,68,1136,90,64,palette.cream,true);
    facts.roadmap.forEach((entry,index)=>{
      const y=215+index*130;
      text(s,`roadmap-${index}-when`,entry.when,76,y,170,44,33,'#E6C1A4',true);
      text(s,`roadmap-${index}-status`,entry.status,76,y+46,170,30,21,'#D7DFD6');
      text(s,`roadmap-${index}-text`,entry.text,270,y,930,84,35,palette.cream);
    });
    text(s,'runtime-fact','Nemotron on Crusoe. Evidence remains attached to every repair.',76,640,1120,44,25,'#D7DFD6');
  } else {
    text(s,'close-title','Memory needs proof',72,100,1136,90,64,palette.cream,true);
    text(s,'memory-value','Only passing repairs enter verified memory.\nFailures remain in the run history.',76,253,1100,154,43,palette.cream);
    text(s,'runtime-fact','Nemotron operates without a runtime teacher.',76,484,1116,76,34,'#E6C1A4',true);
    text(s,'close-product','Recursive Lab',76,626,1070,45,29,'#D7DFD6');
  }
  notes(s,timing.close,facts.script?.close || 'Only a verified repair enters durable memory. The record links the source, the patch and passing checks. Failed attempts remain in the run history. Recursive Lab connects Nemotron on Crusoe to the framework’s own maintenance loop, and leaves evidence we can inspect.',[facts.memory_source || 'examples/self_debug.py',facts.result_source || 'examples/self_debug.py',...(facts.roadmap_sources||[])]);
}

await fs.mkdir(previewsDir,{recursive:true});
for (const [i,s] of p.slides.items.entries()) {
  const png=await p.export({slide:s,format:'png',scale:1});
  await fs.writeFile(path.join(previewsDir,`slide-${i+1}.png`),new Uint8Array(await png.arrayBuffer()));
}
const montage=await p.export({format:'webp',montage:true});
await fs.writeFile(path.join(previewsDir,'contact-sheet.webp'),new Uint8Array(await montage.arrayBuffer()));
const candidatePath=path.join(workspaceDir,'.build','candidate.pptx');
await (await PresentationFile.exportPptx(p)).save(candidatePath);
await fs.writeFile(path.join(workspaceDir,'.build','deck-inspection.ndjson'),(await p.inspect({kind:'slide,textbox,image,notes',maxChars:100000})).ndjson);
if (!draft) {
  let filename=process.env.DECK_FILENAME || 'Recursive-Lab-3-Minute-Demo.pptx';
  if (!process.env.DECK_FILENAME) {
    let revision=2;
    while (await fs.access(path.join(workspaceDir,'output',filename)).then(()=>true).catch(()=>false)) {
      filename=`Recursive-Lab-3-Minute-Demo-v${revision++}.pptx`;
    }
  }
  const result=await finalizePresentation({
    workspaceDir,candidatePath,finalPath:path.join(workspaceDir,'output',filename),
    pythonExecutable:RUNTIME_PYTHON,
    integrityValidatorPath:path.join(SKILL_DIR,'container_tools/inspect_presentation_package_integrity.py'),
    layoutValidatorPath:path.join(SKILL_DIR,'container_tools/inspect_presentation_layout_geometry.py'),
    layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit'],
    explicitTotalSlideCount:5,
    requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],
    fontPolicy:{basis:'design',families:[font]},
    verifyArtifactToolImport:true,
    receiptPath:path.join(workspaceDir,'.build',`${filename}.validation.json`),
  });
  const scriptPath=path.join(workspaceDir,'output',filename.replace('Demo','Script').replace(/\.pptx$/,'.txt'));
  const segments=[[timing.opening+' — SLIDE 1','opening'],[timing.problem+' — SLIDE 2','problem'],[timing.demo+' — SLIDE 3, THEN LOCAL APP','demo'],[timing.results+' — SLIDE 4','results'],[timing.close+' — SLIDE 5','close']];
  const setup=facts.presenter_setup || 'Open http://127.0.0.1:8765/ before the talk. Use Stage view. The walkthrough replays captured inference. Verify repair locally reruns saved checks without a new model API call.';
  const scriptText='RECURSIVE LAB — THREE-MINUTE ENGLISH DEMO\nStage directions in brackets are not spoken.\n\n'+segments.map(([heading,key])=>heading+'\n'+facts.script[key]).join('\n\n')+'\n\nPRESENTER SETUP (NOT SPOKEN)\n'+setup+'\n\nEVIDENCE\n'+facts.result_source+'\n'+(facts.provenance_notes||'')+'\n';
  await fs.writeFile(scriptPath,scriptText);
  const deliverableDeck=path.join(workspaceDir,'Recursive-Lab-3min-demo.pptx');
  const deliverableScript=path.join(workspaceDir,'3-minute-script.txt');
  await fs.copyFile(path.join(workspaceDir,'output',filename),deliverableDeck);
  await fs.copyFile(scriptPath,deliverableScript);
  if (factsPath!==path.join(workspaceDir,'facts.json')) await fs.copyFile(factsPath,path.join(workspaceDir,'facts.json'));
  const spoken=segments.map(([,key])=>facts.script[key].replace(/\[[^\]]*\]/g,'')).join(' ');
  console.log(JSON.stringify({font,finalized:result,deliverableDeck,deliverableScript,spokenWords:spoken.match(/\b[\w’']+\b/g)?.length}));
} else console.log(JSON.stringify({font,draft:true,slides:p.slides.items.length}));
