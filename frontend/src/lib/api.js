export async function api(path, options = {}) {
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers || {})
    }
  })
  const text = await response.text()
  let data = {}
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { raw: text }
    }
  }
  if (!response.ok) {
    const message = data.detail || data.message || response.statusText || '请求失败'
    throw new Error(message)
  }
  return data
}

export function postJson(path, payload) {
  return api(path, {
    method: 'POST',
    body: JSON.stringify(payload || {})
  })
}

export function deleteJson(path) {
  return api(path, {
    method: 'DELETE'
  })
}

export function postFormData(path, formData) {
  return api(path, {
    method: 'POST',
    body: formData
  })
}
