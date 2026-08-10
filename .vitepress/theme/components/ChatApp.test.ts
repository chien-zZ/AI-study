import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ChatApp from './ChatApp.vue'
import {
  createConversation, deleteMessage, getSession, listConversations, listMessages, logoutSession,
  sendFeedback, streamReply, updateConversation,
} from '../chat/api'


vi.mock('../chat/api', async () => {
  class ApiError extends Error {
    constructor(message: string, public status = 0, public retryAfter?: number) { super(message) }
  }
  return {
    ApiError,
    getSession: vi.fn(), redeemInvite: vi.fn(), logoutSession: vi.fn(),
    listConversations: vi.fn(), createConversation: vi.fn(), updateConversation: vi.fn(),
    deleteConversation: vi.fn(), listMessages: vi.fn(), deleteMessage: vi.fn(),
    sendFeedback: vi.fn(), streamReply: vi.fn(),
  }
})

const remoteConversation = {
  id: 'conversation-1', title: 'Vue 问题', persona: 'normal' as const, localOnly: false,
  createdAt: 1, updatedAt: 1,
}

beforeEach(() => {
  vi.mocked(getSession).mockReset()
  vi.mocked(listConversations).mockReset().mockResolvedValue([remoteConversation])
  vi.mocked(listMessages).mockReset().mockResolvedValue([])
  vi.mocked(createConversation).mockReset().mockResolvedValue(remoteConversation)
  vi.mocked(updateConversation).mockReset().mockImplementation(async (_, changes) => ({ ...remoteConversation, ...changes }))
  vi.mocked(deleteMessage).mockReset().mockResolvedValue(undefined)
  vi.mocked(sendFeedback).mockReset().mockResolvedValue(undefined)
  vi.mocked(streamReply).mockReset()
  vi.mocked(logoutSession).mockReset().mockResolvedValue(undefined)
  localStorage.clear()
})

function authenticatedSession() {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true, viewerId: 'viewer-1', expiresAt: 123,
    limits: { minute: 5, day: 50, minuteRemaining: 4, dayRemaining: 49 },
  })
}

describe('ChatApp workspace', () => {
  it('shows only the invite form when unauthenticated', async () => {
    vi.mocked(getSession).mockResolvedValue({ authenticated: false })
    const wrapper = mount(ChatApp)
    await flushPromises()
    expect(wrapper.text()).toContain('请输入邀请码继续')
    expect(wrapper.text()).toContain('可由站点管理员查看')
    expect(wrapper.text()).toContain('不会写入服务器或管理员后台')
    expect(wrapper.find('textarea').exists()).toBe(false)
  })

  it('loads synchronized conversations and messages after authentication', async () => {
    authenticatedSession()
    vi.mocked(listMessages).mockResolvedValue([{ id: 'm1', role: 'user', content: '已同步的问题' }])
    const wrapper = mount(ChatApp)
    await flushPromises()
    expect(wrapper.text()).toContain('Vue 问题')
    expect(wrapper.text()).toContain('已同步的问题')
    expect(wrapper.text()).toContain('今日 49')
  })

  it('defaults to normal persona and switches to Vue assistant without clearing messages', async () => {
    authenticatedSession()
    const wrapper = mount(ChatApp)
    await flushPromises()
    const personaButtons = wrapper.findAll('.persona-switch button')
    expect(personaButtons.map((item) => item.text())).toEqual(['普通助手', 'Vue 框架助手', '雌小鬼亚亚', '斗罗大陆'])

    await personaButtons[1].trigger('click')
    await flushPromises()
    expect(updateConversation).toHaveBeenCalledWith('conversation-1', { persona: 'vue' })
  })

  it('locks persona buttons after the first message and explains how to switch', async () => {
    authenticatedSession()
    vi.mocked(listMessages).mockResolvedValue([{ id: 'm1', role: 'user', content: '已有问题' }])
    const wrapper = mount(ChatApp)
    await flushPromises()

    expect(wrapper.text()).toContain('当前不允许切换人格，如需切换人格请新建对话')
    expect(wrapper.findAll('.persona-switch button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.findAll('.persona-switch button')[1].trigger('click')
    expect(updateConversation).not.toHaveBeenCalled()
  })

  it('streams Vue sources and replaces temporary messages with server ids', async () => {
    authenticatedSession()
    vi.mocked(streamReply).mockImplementation(async (options) => {
      options.onSources?.([{ file: 'docs/watchers.md', documentTitle: '侦听器', sectionTitle: '基本示例', score: 0.9, url: 'https://example.com' }])
      options.onToken('知识库回答')
      return { messages: [
        { id: 'u1', role: 'user', content: options.message },
        { id: 'a1', role: 'assistant', content: '知识库回答' },
      ] }
    })
    const wrapper = mount(ChatApp)
    await flushPromises()
    await wrapper.findAll('.persona-switch button')[1].trigger('click')
    await wrapper.find('textarea').setValue('watch 怎么用？')
    await wrapper.find('.send-button').trigger('click')
    await flushPromises()

    expect(streamReply).toHaveBeenCalledWith(expect.objectContaining({ persona: 'vue', conversationId: 'conversation-1' }))
    expect(wrapper.text()).toContain('知识库回答')
  })

  it('aborts an active request from the stop button', async () => {
    authenticatedSession()
    let receivedSignal: AbortSignal | undefined
    vi.mocked(streamReply).mockImplementation((options) => {
      receivedSignal = options.signal
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))))
    })
    const wrapper = mount(ChatApp)
    await flushPromises()
    await wrapper.find('textarea').setValue('停止测试')
    void wrapper.find('.send-button').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.find('.stop-button').trigger('click')
    await flushPromises()
    expect(receivedSignal?.aborted).toBe(true)
    expect(wrapper.text()).toContain('已停止生成')
  })

  it('rates and deletes a synchronized assistant message', async () => {
    authenticatedSession()
    vi.mocked(listMessages).mockResolvedValue([
      { id: 'u1', role: 'user', content: '问题' },
      { id: 'a1', role: 'assistant', content: '回答' },
    ])
    const wrapper = mount(ChatApp)
    await flushPromises()
    const assistantActions = wrapper.findAll('.message.assistant .message-actions button')

    await assistantActions[2].trigger('click')
    await flushPromises()
    expect(sendFeedback).toHaveBeenCalledWith('a1', 1, undefined)

    await assistantActions[4].trigger('click')
    await flushPromises()
    expect(deleteMessage).toHaveBeenCalledWith('a1')
    expect(wrapper.text()).not.toContain('回答')
  })

  it('logs out without deleting browser-only conversations', async () => {
    authenticatedSession()
    localStorage.setItem('ai-study-local-conversations:v2:viewer-1', JSON.stringify([{ id: 'local-1' }]))
    const wrapper = mount(ChatApp)
    await flushPromises()
    const buttons = wrapper.findAll('.toolbar-meta .icon-button')
    await buttons[buttons.length - 1].trigger('click')
    await flushPromises()
    expect(logoutSession).toHaveBeenCalledOnce()
    expect(localStorage.getItem('ai-study-local-conversations:v2:viewer-1')).not.toBeNull()
    expect(wrapper.text()).toContain('请输入邀请码继续')
  })
})
