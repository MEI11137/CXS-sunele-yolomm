// 模拟数据 —— 仅用于交互原型，明确标识「模拟」
import type {
  Workspace,
  Defect,
  DetectionModel,
  Session,
} from '@/types'

export const mockWorkspaces: Workspace[] = [
  {
    id: 'ws-default',
    name: '默认工作区',
    description: '个人默认工作区，用于海上风电叶片巡检影像与模型管理。',
    requirements: '',
    requirements_raw: {},
    created_at: '2026-09-01T08:00:00Z',
    updated_at: '2026-10-20T08:00:00Z',
  },
  {
    id: 'ws-north-sea',
    name: '北海风场项目',
    description: '北海海上风电场叶片巡检专项，覆盖 18 台风机。',
    requirements: '重点核查前缘侵蚀与表面裂纹。',
    requirements_raw: { notes: '重点核查前缘侵蚀与表面裂纹。' },
    created_at: '2026-09-15T08:00:00Z',
    updated_at: '2026-10-18T08:00:00Z',
  },
]

export const mockDefects: Defect[] = [
  {
    id: 'd1',
    turbineId: 'F03',
    bladeFile: 'F03_A_2026-10-20_1032.jpg',
    thumbnail: '',
    suspectedType: '前缘侵蚀',
    confidence: 0.92,
    reviewStatus: 'pending',
    severity: 'high',
  },
  {
    id: 'd2',
    turbineId: 'F07',
    bladeFile: 'F07_C_2026-10-20_1415.jpg',
    thumbnail: '',
    suspectedType: '表面裂纹',
    confidence: 0.78,
    reviewStatus: 'pending',
    severity: 'medium',
  },
  {
    id: 'd3',
    turbineId: 'F12',
    bladeFile: 'F12_B_2026-10-19_0920.jpg',
    thumbnail: '',
    suspectedType: '涂层剥落',
    confidence: 0.85,
    reviewStatus: 'pending',
    severity: 'medium',
  },
  {
    id: 'd4',
    turbineId: 'F05',
    bladeFile: 'F05_A_2026-10-18_1630.jpg',
    thumbnail: '',
    suspectedType: '前缘侵蚀',
    confidence: 0.61,
    reviewStatus: 'reviewing',
    severity: 'low',
  },
  {
    id: 'd5',
    turbineId: 'F09',
    bladeFile: 'F09_A_2026-10-18_1105.jpg',
    thumbnail: '',
    suspectedType: '表面裂纹',
    confidence: 0.88,
    reviewStatus: 'done',
    severity: 'high',
  },
]

export const mockModels: DetectionModel[] = [
  {
    id: 'm-dwm',
    name: 'DWM-YOLO',
    version: 'v1.3',
    type: 'system',
    status: 'available',
    metrics: { mAP50: 0.942, mAP5095: 0.781, precision: 0.913, recall: 0.876 },
  },
  {
    id: 'm-dwm-base',
    name: 'DWM-YOLO',
    version: 'v1.0',
    type: 'system',
    status: 'available',
    metrics: { mAP50: 0.911, mAP5095: 0.742, precision: 0.884, recall: 0.851 },
  },
  {
    id: 'm-personal-1',
    name: '我的精调模型',
    version: 'v0.3-draft',
    type: 'personal',
    status: 'training',
  },
]

export const mockSessions: Session[] = [
  {
    id: 's1',
    project_id: 'ws-default',
    title: '新会话',
    created_at: Date.now() - 1000 * 60 * 30,
    messages: [
      {
        id: 'm1',
        session_id: 's1',
        role: 'assistant',
        content: '你好，我是海巡助手。我可以协助你发起叶片巡检、核对检测计划，以及处理人工复核。请先选择一个工作区，或直接告诉我你的需求。',
        created_at: Date.now() - 1000 * 60 * 30,
      },
    ],
  },
]

// 用于风机阵列示意与分布图的演示数据（数量由项目数据计算）
export const mockTurbines = {
  count: 18,
  list: [
    { id: 'F01', x: 12, y: 30, status: 'done' },
    { id: 'F02', x: 26, y: 18, status: 'done' },
    { id: 'F03', x: 40, y: 42, status: 'review' },
    { id: 'F04', x: 55, y: 12, status: 'done' },
    { id: 'F05', x: 68, y: 36, status: 'review' },
    { id: 'F06', x: 82, y: 16, status: 'done' },
    { id: 'F07', x: 20, y: 62, status: 'review' },
    { id: 'F08', x: 34, y: 74, status: 'done' },
    { id: 'F09', x: 50, y: 58, status: 'done' },
    { id: 'F10', x: 64, y: 72, status: 'done' },
    { id: 'F11', x: 78, y: 54, status: 'done' },
    { id: 'F12', x: 88, y: 70, status: 'review' },
    { id: 'F13', x: 8, y: 84, status: 'done' },
    { id: 'F14', x: 44, y: 88, status: 'done' },
    { id: 'F15', x: 70, y: 86, status: 'done' },
    { id: 'F16', x: 90, y: 30, status: 'done' },
    { id: 'F17', x: 16, y: 14, status: 'done' },
    { id: 'F18', x: 58, y: 24, status: 'done' },
  ] as { id: string; x: number; y: number; status: 'done' | 'review' }[],
}

export const mockDefectTypes = [
  '前缘侵蚀',
  '表面裂纹',
  '涂层剥落',
  '雷击损伤',
  '结冰附着',
  '其他异常',
]

export const mockSeverities = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
] as const
