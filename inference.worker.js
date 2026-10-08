import { env, pipeline } from 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.2';

const MODEL_ID = 'onnx-community/Qwen2.5-0.5B-Instruct-ONNX';
env.allowLocalModels = false;
env.useBrowserCache = true;
let generatorPromise = null;

async function getGenerator() {
  if (!generatorPromise) {
    const device = self.navigator?.gpu ? 'webgpu' : 'wasm';
    generatorPromise = pipeline('text-generation', MODEL_ID, {
      device,
      dtype: device === 'webgpu' ? 'q4' : 'q8',
      progress_callback: (event) => self.postMessage({type: 'progress', event}),
    }).then((generator) => {
      self.postMessage({type: 'ready', device});
      return generator;
    });
  }
  return generatorPromise;
}

self.onmessage = async (message) => {
  const {type, id, prompt, options} = message.data || {};
  try {
    if (type === 'load') {
      await getGenerator();
      return;
    }
    if (type === 'generate') {
      const generator = await getGenerator();
      const output = await generator(prompt, options || {
        max_new_tokens: 96,
        temperature: 0.25,
        do_sample: true,
        return_full_text: false,
      });
      self.postMessage({type: 'result', id, output});
    }
  } catch (error) {
    self.postMessage({type: 'error', id, message: error?.message || String(error)});
  }
};
