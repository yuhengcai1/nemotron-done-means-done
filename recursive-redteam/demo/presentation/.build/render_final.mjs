import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { FileBlob, Presentation, PresentationFile } from '@oai/artifact-tool';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const presentation=await PresentationFile.importPptx(await FileBlob.load(path.join(root,'Recursive-Lab-3min-demo.pptx')));
for (const [index,slide] of presentation.slides.items.entries()) {
  const png=await presentation.export({slide,format:'png',scale:1});
  await fs.writeFile(path.join(root,'previews',`slide-${index+1}.png`),new Uint8Array(await png.arrayBuffer()));
}
const montage=await presentation.export({format:'webp',montage:true});
await fs.writeFile(path.join(root,'previews','contact-sheet.webp'),new Uint8Array(await montage.arrayBuffer()));
console.log(`Rendered final PPTX: ${presentation.slides.items.length} slides.`);
