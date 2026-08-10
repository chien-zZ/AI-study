<script setup lang="ts">
import {
  Copy, Database, Eye, EyeOff, HardDrive, LogOut, Menu, MessageSquare,
  Pencil, Plus, RotateCcw, Send, Sparkles, Square, ThumbsDown, ThumbsUp, Trash2, X,
} from '@lucide/vue'
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  ApiError, createConversation as createRemoteConversation, deleteConversation as deleteRemoteConversation,
  deleteMessage as deleteRemoteMessage, getSession, listConversations, listMessages, logoutSession,
  redeemInvite, sendFeedback, streamReply, updateConversation,
} from '../chat/api'
import { renderMarkdown } from '../chat/markdown'
import {
  createLocalConversation, loadActiveConversationId, loadLocalConversations, prepareHistory,
  saveActiveConversationId, saveLocalConversations,
} from '../chat/storage'
import type { AuthState, ChatMessage, Conversation, PersonaId } from '../chat/types'


// 认证模块：会话检查完成前保持单一加载状态，避免邀请码表单闪烁。
const loadingSession = ref(true)
const auth = ref<AuthState>({ authenticated: false })
const inviteCode = ref('')
const showInvite = ref(false)
const authBusy = ref(false)
const authError = ref('')

// 会话模块：同步会话和本机会话共享列表，但使用各自的持久化通道。
const conversations = ref<Conversation[]>([])
const activeConversationId = ref('')
const messages = ref<ChatMessage[]>([])
const loadingConversation = ref(false)
const createLocalOnly = ref(false)
const sidebarOpen = ref(false)
const editingTitle = ref(false)
const titleDraft = ref('')

// 生成模块：临时消息先进入响应式列表，完成后再替换为服务端消息 ID。
const input = ref('')
const generating = ref(false)
const chatError = ref('')
const lastFailedQuestion = ref('')
const messageList = ref<HTMLElement | null>(null)
let controller: AbortController | null = null

const viewerId = computed(() => auth.value.viewerId || '')
const activeConversation = computed(() => (
  conversations.value.find((item) => item.id === activeConversationId.value)
))
const persona = computed(() => activeConversation.value?.persona || 'normal')
const canSend = computed(() => input.value.trim().length > 0 && !generating.value && !loadingConversation.value)

function personaLabel(value: PersonaId): string {
  // 角色展示模块：集中维护会话标题区和消息标签使用的角色名称。
  return { normal: '普通助手', vue: 'Vue 框架助手', brat: '雌小鬼亚亚', douluo_dalu: '斗罗大陆' }[value]
}

function formatError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 && error.retryAfter) {
      return `${error.message}（约 ${error.retryAfter} 秒后可重试）`
    }
    return error.message
  }
  return '网络连接失败，请稍后重试'
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

function persistLocalState(): void {
  if (!viewerId.value) return
  const current = activeConversation.value
  if (current?.localOnly) {
    current.messages = messages.value
    current.updatedAt = Math.floor(Date.now() / 1000)
  }
  saveLocalConversations(viewerId.value, conversations.value)
}

async function selectConversation(conversationId: string): Promise<void> {
  if (generating.value) controller?.abort()
  const target = conversations.value.find((item) => item.id === conversationId)
  if (!target) return
  activeConversationId.value = conversationId
  saveActiveConversationId(viewerId.value, conversationId)
  sidebarOpen.value = false
  chatError.value = ''
  loadingConversation.value = true
  try {
    messages.value = target.localOnly ? [...(target.messages || [])] : await listMessages(target.id)
  } catch (error) {
    chatError.value = formatError(error)
    messages.value = []
  } finally {
    loadingConversation.value = false
    await scrollToBottom()
  }
}

async function addConversation(localOnly = createLocalOnly.value): Promise<void> {
  const conversation = localOnly
    ? createLocalConversation('normal')
    : await createRemoteConversation('normal')
  conversations.value.unshift(conversation)
  persistLocalState()
  await selectConversation(conversation.id)
}

async function loadWorkspace(): Promise<void> {
  const [remote, local] = await Promise.all([
    listConversations(),
    Promise.resolve(loadLocalConversations(viewerId.value)),
  ])
  conversations.value = [...remote, ...local].sort((a, b) => b.updatedAt - a.updatedAt)
  if (conversations.value.length === 0) {
    await addConversation(false)
    return
  }
  const saved = loadActiveConversationId(viewerId.value)
  const target = conversations.value.some((item) => item.id === saved)
    ? saved
    : conversations.value[0].id
  await selectConversation(target)
}

async function submitInvite(): Promise<void> {
  if (!inviteCode.value || authBusy.value) return
  authBusy.value = true
  authError.value = ''
  try {
    auth.value = await redeemInvite(inviteCode.value)
    inviteCode.value = ''
    await loadWorkspace()
  } catch (error) {
    authError.value = formatError(error)
  } finally {
    authBusy.value = false
  }
}

async function selectPersona(nextPersona: PersonaId): Promise<void> {
  const current = activeConversation.value
  if (!current || current.persona === nextPersona || generating.value || messages.value.length > 0) return
  if (current.localOnly) {
    current.persona = nextPersona
    current.updatedAt = Math.floor(Date.now() / 1000)
    persistLocalState()
  } else {
    const updated = await updateConversation(current.id, { persona: nextPersona })
    current.persona = updated.persona
    current.updatedAt = updated.updatedAt
  }
}

async function sendMessage(questionOverride?: string): Promise<void> {
  const question = (questionOverride ?? input.value).trim()
  const current = activeConversation.value
  if (!question || generating.value || !current) return

  const previousHistory = current.localOnly ? prepareHistory(messages.value) : []
  const userMessage: ChatMessage = { id: `temp-user-${Date.now()}`, role: 'user', content: question }
  const assistantMessage: ChatMessage = { id: `temp-assistant-${Date.now()}`, role: 'assistant', content: '', sources: [] }
  messages.value.push(userMessage, assistantMessage)
  const userIndex = messages.value.length - 2
  const assistantIndex = messages.value.length - 1
  input.value = ''
  chatError.value = ''
  lastFailedQuestion.value = ''
  generating.value = true
  controller = new AbortController()
  await scrollToBottom()

  try {
    const result = await streamReply({
      message: question,
      history: previousHistory,
      persona: current.persona,
      conversationId: current.localOnly ? undefined : current.id,
      signal: controller.signal,
      onToken(text) {
        messages.value[assistantIndex].content += text
        void scrollToBottom()
      },
      onSources(sources) {
        messages.value[assistantIndex].sources = sources
      },
    })
    if (!current.localOnly && result.messages.length === 2) {
      messages.value.splice(userIndex, 2, ...result.messages)
    }
    if (current.title === '新对话') current.title = question.replace(/\s+/g, ' ').slice(0, 28)
    current.messages = current.localOnly ? messages.value.slice(-20) : undefined
    current.updatedAt = Math.floor(Date.now() / 1000)
    conversations.value.sort((a, b) => b.updatedAt - a.updatedAt)
    persistLocalState()
    if (auth.value.limits) {
      auth.value.limits.minuteRemaining = Math.max(0, auth.value.limits.minuteRemaining - 1)
      auth.value.limits.dayRemaining = Math.max(0, auth.value.limits.dayRemaining - 1)
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      if (current.localOnly && messages.value[assistantIndex]?.content) persistLocalState()
      else {
        messages.value.splice(userIndex, 2)
        input.value = question
      }
      chatError.value = '已停止生成'
    } else {
      messages.value.splice(userIndex, 2)
      input.value = question
      lastFailedQuestion.value = question
      chatError.value = formatError(error)
      if (error instanceof ApiError && error.status === 401) auth.value = { authenticated: false }
    }
  } finally {
    generating.value = false
    controller = null
  }
}

async function removeMessagesFrom(index: number): Promise<void> {
  const current = activeConversation.value
  if (!current) return
  const removed = messages.value.slice(index)
  if (!current.localOnly) {
    for (const item of removed.filter((entry) => entry.id && !entry.id.startsWith('temp-'))) {
      await deleteRemoteMessage(item.id!)
    }
  }
  messages.value.splice(index)
  persistLocalState()
}

async function editUserMessage(index: number): Promise<void> {
  const question = messages.value[index]?.content
  if (!question || generating.value) return
  await removeMessagesFrom(index)
  input.value = question
  await nextTick()
  document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus()
}

async function regenerateAnswer(index: number): Promise<void> {
  const userIndex = index - 1
  const question = messages.value[userIndex]?.role === 'user' ? messages.value[userIndex].content : ''
  if (!question || generating.value) return
  await removeMessagesFrom(userIndex)
  await sendMessage(question)
}

async function removeSingleMessage(index: number): Promise<void> {
  const message = messages.value[index]
  const current = activeConversation.value
  if (!message || !current || generating.value) return
  if (!current.localOnly && message.id && !message.id.startsWith('temp-')) await deleteRemoteMessage(message.id)
  messages.value.splice(index, 1)
  persistLocalState()
}

async function rateMessage(message: ChatMessage, rating: -1 | 1): Promise<void> {
  const current = activeConversation.value
  if (!current || !message.id) return
  message.feedback = message.feedback === rating ? undefined : rating
  let comment: string | undefined
  if (message.feedback === -1) {
    comment = window.prompt('可以补充说明哪里需要改进（可留空）')?.trim() || undefined
  }
  if (!current.localOnly && message.feedback) await sendFeedback(message.id, message.feedback, comment)
  persistLocalState()
}

async function copyText(content: string): Promise<void> {
  await navigator.clipboard.writeText(content)
}

async function handleMessageClick(event: MouseEvent): Promise<void> {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('.code-copy')
  if (!button) return
  const code = button.parentElement?.querySelector('code')?.textContent || ''
  await copyText(code)
  button.textContent = '已复制'
  window.setTimeout(() => { button.textContent = '复制' }, 1200)
}

async function clearMessages(): Promise<void> {
  if (messages.value.length === 0 || generating.value) return
  await removeMessagesFrom(0)
  chatError.value = ''
}

async function removeConversation(conversation: Conversation): Promise<void> {
  if (!window.confirm(`删除“${conversation.title}”？`)) return
  if (!conversation.localOnly) await deleteRemoteConversation(conversation.id)
  conversations.value = conversations.value.filter((item) => item.id !== conversation.id)
  persistLocalState()
  if (activeConversationId.value === conversation.id) {
    if (conversations.value.length) await selectConversation(conversations.value[0].id)
    else await addConversation(false)
  }
}

function beginRename(): void {
  if (!activeConversation.value) return
  titleDraft.value = activeConversation.value.title
  editingTitle.value = true
}

async function commitRename(): Promise<void> {
  const current = activeConversation.value
  const title = titleDraft.value.trim().slice(0, 80)
  if (!current || !title) return
  current.title = title
  editingTitle.value = false
  if (current.localOnly) persistLocalState()
  else await updateConversation(current.id, { title })
}

async function logout(): Promise<void> {
  controller?.abort()
  try { await logoutSession() } finally {
    auth.value = { authenticated: false }
    conversations.value = []
    messages.value = []
  }
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void sendMessage()
  }
}

onMounted(async () => {
  try {
    auth.value = await getSession()
    if (auth.value.authenticated) await loadWorkspace()
  } catch (error) {
    authError.value = formatError(error)
  } finally {
    loadingSession.value = false
  }
})
</script>

<template>
  <section class="chat-shell" aria-label="AI Chat">
    <div v-if="loadingSession" class="state-panel" role="status">
      <Sparkles :size="20" aria-hidden="true" /><span>正在检查访问权限…</span>
    </div>

    <form v-else-if="!auth.authenticated" class="invite-card" @submit.prevent="submitInvite">
      <div class="invite-mark" aria-hidden="true"><Sparkles :size="24" /></div>
      <h1>AI Chat</h1>
      <p>请输入邀请码继续</p>
      <p class="privacy-note">同步会话正文默认保存 365 天，并可由站点管理员查看。选择“新会话仅本机”后，正文不会写入服务器或管理员后台。</p>
      <label for="invite-code">邀请码</label>
      <div class="invite-input-row">
        <input id="invite-code" v-model="inviteCode" :type="showInvite ? 'text' : 'password'" autocomplete="off" minlength="16" maxlength="64" placeholder="请输入邀请码" required>
        <button type="button" class="icon-button" :aria-label="showInvite ? '隐藏邀请码' : '显示邀请码'" :title="showInvite ? '隐藏邀请码' : '显示邀请码'" @click="showInvite = !showInvite">
          <EyeOff v-if="showInvite" :size="18" /><Eye v-else :size="18" />
        </button>
      </div>
      <p v-if="authError" class="error-text" role="alert">{{ authError }}</p>
      <button class="primary-button invite-submit" type="submit" :disabled="authBusy || !inviteCode">{{ authBusy ? '验证中…' : '验证并进入' }}</button>
    </form>

    <div v-else class="chat-workspace">
      <button v-if="sidebarOpen" class="sidebar-backdrop" aria-label="关闭会话列表" @click="sidebarOpen = false" />
      <aside class="conversation-sidebar" :class="{ open: sidebarOpen }">
        <div class="sidebar-header">
          <strong>会话</strong>
          <button class="icon-button" type="button" title="新建会话" aria-label="新建会话" @click="addConversation()"><Plus :size="18" /></button>
        </div>
        <nav class="conversation-list" aria-label="会话列表">
          <div v-for="conversation in conversations" :key="conversation.id" class="conversation-row" :class="{ active: conversation.id === activeConversationId }">
            <button class="conversation-select" type="button" @click="selectConversation(conversation.id)">
              <HardDrive v-if="conversation.localOnly" :size="14" /><MessageSquare v-else :size="14" />
              <span><strong>{{ conversation.title }}</strong><small>{{ personaLabel(conversation.persona) }}</small></span>
            </button>
            <button class="conversation-delete" type="button" title="删除会话" aria-label="删除会话" @click="removeConversation(conversation)"><X :size="14" /></button>
          </div>
        </nav>
        <label class="local-toggle"><input v-model="createLocalOnly" type="checkbox"><HardDrive :size="15" /><span>新会话仅本机</span></label>
      </aside>

      <main class="chat-main">
        <header class="chat-toolbar">
          <button class="icon-button mobile-menu" type="button" title="会话列表" aria-label="会话列表" @click="sidebarOpen = true"><Menu :size="19" /></button>
          <div class="chat-identity">
            <span class="identity-icon" aria-hidden="true"><Database v-if="persona === 'vue'" :size="18" /><Sparkles v-else :size="18" /></span>
            <div class="title-area">
              <form v-if="editingTitle" class="title-form" @submit.prevent="commitRename"><input v-model="titleDraft" maxlength="80" @blur="commitRename"></form>
              <button v-else class="title-button" type="button" title="重命名会话" @click="beginRename"><span>{{ activeConversation?.title || 'AI Chat' }}</span><Pencil :size="12" /></button>
              <p>{{ activeConversation?.localOnly ? '仅保存在本机' : '已同步' }}</p>
            </div>
          </div>

          <div class="persona-controls">
            <div class="persona-switch" aria-label="人设选择">
              <button :class="{ active: persona === 'normal' }" :disabled="messages.length > 0 || generating" :title="messages.length > 0 ? '当前不允许切换人格，如需切换人格请新建对话' : ''" type="button" @click="selectPersona('normal')">普通助手</button>
              <button :class="{ active: persona === 'vue' }" :disabled="messages.length > 0 || generating" :title="messages.length > 0 ? '当前不允许切换人格，如需切换人格请新建对话' : ''" type="button" @click="selectPersona('vue')">Vue 框架助手</button>
              <button :class="{ active: persona === 'brat' }" :disabled="messages.length > 0 || generating" :title="messages.length > 0 ? '当前不允许切换人格，如需切换人格请新建对话' : ''" type="button" @click="selectPersona('brat')">雌小鬼亚亚</button>
              <button :class="{ active: persona === 'douluo_dalu' }" :disabled="messages.length > 0 || generating" :title="messages.length > 0 ? '当前不允许切换人格，如需切换人格请新建对话' : ''" type="button" @click="selectPersona('douluo_dalu')">斗罗大陆</button>
            </div>
            <small v-if="messages.length > 0" class="persona-lock-notice">当前不允许切换人格，如需切换人格请新建对话</small>
          </div>

          <div class="toolbar-meta">
            <div v-if="auth.limits" class="quota" title="邀请码剩余额度"><span>今日 {{ auth.limits.dayRemaining }}</span><span>本分钟 {{ auth.limits.minuteRemaining }}</span></div>
            <button class="icon-button" type="button" aria-label="清空对话" title="清空对话" @click="clearMessages"><Trash2 :size="17" /></button>
            <button class="icon-button" type="button" aria-label="退出登录" title="退出登录" @click="logout"><LogOut :size="17" /></button>
          </div>
        </header>

        <div ref="messageList" class="message-list" aria-live="polite" @click="handleMessageClick">
          <div v-if="loadingConversation" class="empty-state"><span>正在加载…</span></div>
          <div v-else-if="messages.length === 0" class="empty-state">
            <span class="empty-icon" aria-hidden="true"><Database v-if="persona === 'vue'" :size="22" /><Sparkles v-else :size="22" /></span>
            <strong>{{ persona === 'vue' ? '可以开始询问 Vue 框架问题' : persona === 'brat' ? '大叔，想聊点什么？' : persona === 'douluo_dalu' ? '欢迎来到斗罗大陆' : '有什么想问的？' }}</strong>
          </div>
          <article v-for="(message, index) in messages" :key="message.id || index" class="message" :class="message.role">
            <span class="message-label">{{ message.role === 'user' ? '你' : personaLabel(persona) }}</span>
            <div v-if="message.role === 'assistant'" class="message-content markdown-body" v-html="renderMarkdown(message.content || '…')" />
            <div v-else class="message-content">{{ message.content }}</div>
            <div v-if="message.sources?.length" class="source-list">
              <a v-for="(source, sourceIndex) in message.sources" :key="`${source.file}-${sourceIndex}`" :href="source.url" target="_blank" rel="noopener noreferrer">
                <span>[{{ sourceIndex + 1 }}] {{ source.sectionTitle || source.documentTitle }}</span><small>{{ source.file }}</small>
              </a>
            </div>
            <div v-if="!generating || index < messages.length - 1" class="message-actions">
              <button type="button" title="复制" aria-label="复制消息" @click="copyText(message.content)"><Copy :size="14" /></button>
              <button v-if="message.role === 'user'" type="button" title="编辑并重新提问" aria-label="编辑消息" @click="editUserMessage(index)"><Pencil :size="14" /></button>
              <button v-if="message.role === 'assistant'" type="button" title="重新生成" aria-label="重新生成" @click="regenerateAnswer(index)"><RotateCcw :size="14" /></button>
              <button v-if="message.role === 'assistant'" type="button" title="有帮助" aria-label="有帮助" :class="{ selected: message.feedback === 1 }" @click="rateMessage(message, 1)"><ThumbsUp :size="14" /></button>
              <button v-if="message.role === 'assistant'" type="button" title="需改进" aria-label="需改进" :class="{ selected: message.feedback === -1 }" @click="rateMessage(message, -1)"><ThumbsDown :size="14" /></button>
              <button type="button" title="删除" aria-label="删除消息" @click="removeSingleMessage(index)"><Trash2 :size="14" /></button>
            </div>
          </article>
        </div>

        <footer class="composer-panel">
          <div v-if="chatError" class="chat-error" role="status"><span>{{ chatError }}</span><button v-if="lastFailedQuestion" type="button" @click="sendMessage(lastFailedQuestion)"><RotateCcw :size="14" />重试</button></div>
          <div class="composer">
            <textarea v-model="input" maxlength="4000" rows="2" :disabled="generating" placeholder="输入消息…" @keydown="handleKeydown" />
            <button v-if="generating" class="stop-button composer-button" type="button" aria-label="停止生成" title="停止生成" @click="controller?.abort()"><Square :size="17" fill="currentColor" /></button>
            <button v-else class="primary-button send-button composer-button" type="button" :disabled="!canSend" aria-label="发送消息" title="发送消息" @click="sendMessage()"><Send :size="18" /></button>
          </div>
        </footer>
      </main>
    </div>
  </section>
</template>

<style scoped>
:global(.VPPage:has(.chat-shell)) { padding: 0 !important; }
:global(.VPContent:has(.chat-shell)) { overflow: hidden; }
* { box-sizing: border-box; }
.chat-shell { width: min(1280px, 100%); height: calc(100dvh - var(--vp-nav-height)); min-height: 520px; margin: 0 auto; padding: 12px 20px 16px; }
.state-panel { height: 100%; display: flex; align-items: center; justify-content: center; gap: 10px; color: var(--vp-c-text-2); }
.invite-card { width: min(420px, calc(100% - 32px)); margin: clamp(36px, 12vh, 110px) auto 0; padding: 30px; border: 1px solid var(--vp-c-divider); border-radius: 8px; background: var(--vp-c-bg); box-shadow: 0 18px 44px rgba(0,0,0,.08); }
.invite-mark,.identity-icon,.empty-icon { display: inline-grid; place-items: center; color: var(--vp-c-brand-1); }
.invite-mark { width: 42px; height: 42px; border-radius: 8px; background: var(--vp-c-brand-soft); }
.invite-card h1 { margin: 16px 0 4px; border: 0; font-size: 24px; }
.invite-card p { margin: 0; color: var(--vp-c-text-2); }
.invite-card .privacy-note { margin-top: 12px; padding: 9px 10px; border-radius: 6px; background: var(--vp-c-bg-soft); color: var(--vp-c-text-3); font-size: 12px; line-height: 1.55; }
.invite-card label { display: block; margin: 22px 0 8px; font-size: 13px; font-weight: 600; }
.invite-input-row { display: flex; gap: 8px; }
input,textarea { width: 100%; border: 1px solid var(--vp-c-divider); border-radius: 7px; background: var(--vp-c-bg); color: var(--vp-c-text-1); font: inherit; letter-spacing: 0; }
input { min-width: 0; height: 40px; padding: 0 11px; }
textarea { height: 64px; min-height: 64px; max-height: 120px; resize: vertical; padding: 10px 12px; line-height: 1.45; }
input:focus,textarea:focus { outline: 2px solid var(--vp-c-brand-soft); border-color: var(--vp-c-brand-1); }
button { border: 0; border-radius: 7px; font: inherit; font-weight: 600; letter-spacing: 0; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .5; }
.primary-button { background: var(--vp-c-brand-1); color: white; }
.invite-submit { width: 100%; height: 42px; margin-top: 14px; }
.icon-button { width: 36px; height: 36px; display: inline-grid; flex: 0 0 auto; place-items: center; padding: 0; background: transparent; color: var(--vp-c-text-2); }
.icon-button:hover { background: var(--vp-c-bg-soft); color: var(--vp-c-text-1); }
.invite-input-row .icon-button { width: 42px; height: 42px; border: 1px solid var(--vp-c-divider); }
.error-text { margin: 9px 0 0 !important; color: var(--vp-c-danger-1) !important; font-size: 13px; }
.chat-workspace { height: 100%; min-height: 0; display: grid; grid-template-columns: 238px minmax(0,1fr); overflow: hidden; border: 1px solid var(--vp-c-divider); border-radius: 8px; background: var(--vp-c-bg); }
.conversation-sidebar { min-height: 0; display: flex; flex-direction: column; border-right: 1px solid var(--vp-c-divider); background: var(--vp-c-bg-soft); }
.sidebar-header { height: 58px; display: flex; align-items: center; justify-content: space-between; padding: 0 12px 0 15px; border-bottom: 1px solid var(--vp-c-divider); }
.conversation-list { flex: 1; min-height: 0; overflow-y: auto; padding: 8px; }
.conversation-row { position: relative; display: flex; align-items: stretch; margin-bottom: 3px; border-radius: 6px; }
.conversation-row:hover,.conversation-row.active { background: var(--vp-c-bg); }
.conversation-row.active { box-shadow: inset 3px 0 var(--vp-c-brand-1); }
.conversation-select { min-width: 0; flex: 1; display: flex; align-items: center; gap: 9px; padding: 9px 30px 9px 10px; background: transparent; color: var(--vp-c-text-2); text-align: left; }
.conversation-select > span { min-width: 0; display: grid; gap: 2px; }
.conversation-select strong,.conversation-select small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-select strong { color: var(--vp-c-text-1); font-size: 13px; }
.conversation-select small { color: var(--vp-c-text-3); font-size: 10px; font-weight: 400; }
.conversation-delete { position: absolute; right: 5px; top: 50%; width: 25px; height: 25px; display: none; place-items: center; transform: translateY(-50%); background: transparent; color: var(--vp-c-text-3); }
.conversation-row:hover .conversation-delete { display: grid; }
.local-toggle { min-height: 48px; display: flex; align-items: center; gap: 7px; padding: 0 14px; border-top: 1px solid var(--vp-c-divider); color: var(--vp-c-text-2); font-size: 12px; cursor: pointer; }
.local-toggle input { width: 15px; height: 15px; padding: 0; accent-color: var(--vp-c-brand-1); }
.chat-main { min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.chat-toolbar { min-height: 62px; display: grid; grid-template-columns: minmax(180px,1fr) auto minmax(170px,1fr); align-items: center; gap: 16px; padding: 8px 14px; border-bottom: 1px solid var(--vp-c-divider); }
.mobile-menu { display: none; }
.chat-identity { min-width: 0; display: flex; align-items: center; gap: 9px; }
.identity-icon { width: 34px; height: 34px; flex: 0 0 auto; border-radius: 7px; background: var(--vp-c-brand-soft); }
.title-area { min-width: 0; }
.title-area p { margin: 2px 0 0; color: var(--vp-c-text-3); font-size: 10px; }
.title-button { max-width: 100%; display: flex; align-items: center; gap: 5px; padding: 0; background: transparent; color: var(--vp-c-text-1); font-size: 14px; }
.title-button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.title-form input { width: min(220px,100%); height: 30px; }
.persona-controls { min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 4px; }
.persona-switch { height: 36px; display: flex; align-items: center; gap: 2px; padding: 3px; border: 1px solid var(--vp-c-divider); border-radius: 9px; background: var(--vp-c-bg-soft); }
.persona-switch button { height: 28px; padding: 0 10px; background: transparent; color: var(--vp-c-text-2); font-size: 12px; white-space: nowrap; }
.persona-switch button.active { background: var(--vp-c-bg); color: var(--vp-c-brand-1); box-shadow: 0 1px 3px rgba(0,0,0,.1); }
.persona-switch button:disabled { opacity: .55; cursor: not-allowed; }
.persona-lock-notice { max-width: 100%; color: var(--vp-c-text-3); font-size: 11px; line-height: 1.35; text-align: center; white-space: nowrap; }
.toolbar-meta { min-width: 0; display: flex; align-items: center; justify-content: flex-end; gap: 3px; }
.quota { display: flex; gap: 8px; margin-right: 5px; color: var(--vp-c-text-3); font-size: 10px; white-space: nowrap; }
.message-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 20px clamp(16px,4vw,52px); scroll-behavior: smooth; }
.empty-state { height: 100%; display: grid; place-content: center; justify-items: center; gap: 10px; color: var(--vp-c-text-3); text-align: center; }
.empty-icon { width: 42px; height: 42px; border-radius: 50%; background: var(--vp-c-bg-soft); }
.empty-state strong { color: var(--vp-c-text-2); font-size: 15px; }
.message { width: fit-content; max-width: min(82%,760px); margin-bottom: 16px; }
.message.user { margin-left: auto; }
.message-label { display: block; margin: 0 3px 5px; color: var(--vp-c-text-3); font-size: 10px; }
.message.user .message-label { text-align: right; }
.message-content { border-radius: 8px; padding: 10px 13px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.message.user .message-content { background: var(--vp-c-brand-1); color: white; }
.message.assistant .message-content { border: 1px solid var(--vp-c-divider); background: var(--vp-c-bg-soft); }
.markdown-body { white-space: normal; }
.markdown-body :deep(p) { margin: 0 0 8px; }
.markdown-body :deep(p:last-child) { margin-bottom: 0; }
.markdown-body :deep(.code-block) { position: relative; }
.markdown-body :deep(pre) { overflow-x: auto; border-radius: 6px; padding: 12px; background: var(--vp-code-block-bg); }
.markdown-body :deep(.code-copy) { position: absolute; z-index: 1; top: 6px; right: 6px; height: 26px; padding: 0 8px; background: var(--vp-c-bg); color: var(--vp-c-text-2); font-size: 11px; }
.source-list { display: grid; gap: 5px; margin-top: 7px; }
.source-list a { display: grid; padding: 7px 9px; border-left: 2px solid var(--vp-c-brand-1); background: var(--vp-c-bg-soft); color: var(--vp-c-text-2); font-size: 11px; text-decoration: none; }
.source-list a:hover span { color: var(--vp-c-brand-1); }
.source-list small { color: var(--vp-c-text-3); }
.message-actions { min-height: 26px; display: flex; justify-content: flex-start; gap: 1px; margin-top: 3px; opacity: 0; transition: opacity .15s; }
.message.user .message-actions { justify-content: flex-end; }
.message:hover .message-actions,.message-actions:focus-within { opacity: 1; }
.message-actions button { width: 27px; height: 27px; display: grid; place-items: center; padding: 0; background: transparent; color: var(--vp-c-text-3); }
.message-actions button:hover,.message-actions button.selected { background: var(--vp-c-bg-soft); color: var(--vp-c-brand-1); }
.composer-panel { flex: 0 0 auto; padding: 9px 13px 11px; border-top: 1px solid var(--vp-c-divider); background: var(--vp-c-bg-soft); }
.chat-error { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 7px; color: var(--vp-c-danger-1); font-size: 12px; }
.chat-error button { display: flex; align-items: center; gap: 4px; padding: 4px 7px; background: var(--vp-c-danger-soft); color: var(--vp-c-danger-1); font-size: 11px; }
.composer { display: flex; align-items: center; gap: 8px; }
.composer-button { width: 42px; height: 42px; min-width: 42px; display: inline-grid; place-items: center; margin: 0; padding: 0; }
.stop-button { background: var(--vp-c-danger-soft); color: var(--vp-c-danger-1); }
.sidebar-backdrop { display: none; }
@media (max-width: 920px) {
  .chat-toolbar { grid-template-columns: minmax(150px,1fr) auto; }
  .persona-controls { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  .toolbar-meta { grid-column: 2; grid-row: 1; }
  .quota { display: none; }
}
@media (max-width: 760px) {
  .chat-shell { min-height: 440px; padding: 0; }
  .chat-workspace { grid-template-columns: 1fr; border-right: 0; border-left: 0; border-radius: 0; }
  .conversation-sidebar { position: absolute; z-index: 20; inset: 0 auto 0 0; width: min(286px,85vw); transform: translateX(-101%); transition: transform .18s; box-shadow: 12px 0 30px rgba(0,0,0,.16); }
  .conversation-sidebar.open { transform: translateX(0); }
  .sidebar-backdrop { position: absolute; z-index: 19; inset: 0; display: block; border-radius: 0; background: rgba(0,0,0,.28); }
  .chat-toolbar { min-height: 58px; grid-template-columns: auto minmax(0,1fr) auto; gap: 5px; padding: 7px 8px; }
  .mobile-menu { display: inline-grid; }
  .persona-controls { grid-column: 1 / -1; grid-row: 2; width: 100%; }
  .persona-switch { width: 100%; }
  .persona-switch button { min-width: 0; flex: 1; padding: 0 5px; }
  .chat-identity { min-width: 0; }
  .identity-icon,.title-area p { display: none; }
  .toolbar-meta { grid-column: 3; grid-row: 1; }
  .message-list { padding: 14px 11px; }
  .message { max-width: 92%; }
  .message-actions { opacity: 1; }
  .composer-panel { padding: 8px 9px 10px; }
  textarea { height: 58px; min-height: 58px; }
}
</style>
